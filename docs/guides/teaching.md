# Teaching with Loom

Loom can serve as the lab environment for a course on AI agents, information retrieval,
databases or software engineering. Students run it on a laptop with Docker, or in a GitHub
codespace with nothing installed, and every lab below uses pages from these docs, so the
steps stay current with each release.

## Setup

| Students have                  | Start with                                                                                                |
| ------------------------------ | --------------------------------------------------------------------------------------------------------- |
| A GitHub account and a browser | [Try it in GitHub Codespaces](../getting-started/quickstart.md#try-it-in-github-codespaces)               |
| Docker on their own machine    | [Quickstart: fast path](../getting-started/quickstart.md#fast-path-released-image-no-clone)               |
| A shared server for the class  | [Start in multi-user mode](../getting-started/quickstart.md#start-in-multi-user-mode), one namespace each |

The stock install needs no API key: it embeds with a bundled local model, so search works
offline, and no write verdict calls an LLM. Extraction, summaries and generated answers
(`memory.ask`, `knowledge.synthesize`) need a real LLM
([models and API keys](../getting-started/models-and-keys.md)); the exercises below use
writes, reads and search only.

The `loom` commands below run inside the stack's `loom` container:
`docker compose --project-directory "$HOME/.loom" exec loom loom <command>` on a laptop
(in a codespace, the install directory the creation log prints). On a shared server the
instructor runs them.

## Lab 1: an agent that remembers

Build a small agent with the Python SDK: an identity, facts it saves and reads back, a
session, and a run. Follow [Your first agent](../getting-started/first-agent.md).

Exercises:

- Ask a question no saved fact answers and explain the abstain code the reply carries
  ([search and ask](../features/memory/search-and-ask.md)).
- Save a fact, then a close but different value under another key, and read the write
  verdict ([how a write is judged](../features/memory/conflicts-and-refusal.md#how-a-write-is-judged)).
- Revise a fact and read its versions with `memory.history`
  ([history and provenance](../features/memory/history-and-provenance.md#versions-memoryhistory)).

## Lab 2: document search with citations

Ingest a document with `loom demo --file <path>`, which also prints the top search hits for
a question ([ask a question about a document](../getting-started/quickstart.md#optional-ask-a-question-about-a-document));
the container holds these docs under `/app/docs/`. Then search the namespace's documents
with `knowledge.search` over HTTP or the SDK ([knowledge documents](../features/knowledge.md)).

Exercises:

- For ten questions, label the document and passage that answers each, and measure
  recall@k on `knowledge.search` ([evaluate your workload](../getting-started/evaluate.md#memory-and-rag)).
- Add questions the documents do not answer, and measure how often a labelled passage
  still appears in the top k.

## Lab 3: a code index

The instructor indexes a repository
([index a repository](../getting-started/quickstart.md#optional-index-a-repository-on-the-released-stack));
on a shared server, an entry marked `"public": true` lets any member of the org join its
index as a reader. Students
ask where a symbol is defined, who calls it, and what a change to it touches
([what the code index answers](../features/code-index/overview.md#what-it-answers)), over
HTTP or the SDK. A coding agent can ask the same questions once connected
([connect a coding agent](../getting-started/connect-a-coding-agent.md)); it needs its own
model account.

Exercises:

- Pick five functions and compare `code.callers` with what `grep` finds; explain each
  difference ([reading an answer](../features/code-index/overview.md#reading-an-answer)).
- Change a function on a branch and read what `code.impact` reports for the diff.

## Lab 4: memory as a database

Loom keeps its data as rows in ClickHouse tables, and a read-only ClickHouse user reads a
namespace through its views ([SQL surface](../reference/sql-surface.md)). The instructor
makes one user per student, for that student's namespace:

```bash
loom sql-access grant --user student1 --org dev --namespaces local
```

On a laptop or in a codespace, the student then queries with the stack's client:
`docker compose --project-directory "$HOME/.loom" exec clickhouse clickhouse-client --user student1 --password <password>`.
On a shared server, students need a route to ClickHouse the instructor provides.

Exercises:

- Write a query over `loom.sql_v1_memories` for the facts saved in the last hour, by
  agent.
- Compare a `memory.search` result with a SQL query over the same rows and explain what
  ranking adds.

## Which labs fit which course

| Course                | Labs    | Read next                                                                                                 |
| --------------------- | ------- | --------------------------------------------------------------------------------------------------------- |
| AI agents             | 1, 2, 3 | [Integrate Loom into an agent](agent-integration.md), [memory](../concepts/memory.md)                     |
| Information retrieval | 1, 2    | [Search and ask](../features/memory/search-and-ask.md), [evaluate Loom](../getting-started/evaluate.md)   |
| Databases             | 1, 4    | [Storage authority](../architecture/storage-authority.md), [SQL surface](../reference/sql-surface.md)     |
| Software engineering  | 3       | [Code index](../features/code-index/overview.md), [PR history](../features/code-index/history-and-prs.md) |
