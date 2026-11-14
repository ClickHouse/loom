# Teaching with Loom

Loom can serve as the lab environment for a course on AI agents, information retrieval and
databases, or software engineering. Students run it on a laptop or in a GitHub codespace
with nothing installed, and every lab below uses pages from these docs, so the steps stay
current with each release.

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

Ingest a document and ask a question about it
([ask a question about a document](../getting-started/quickstart.md#optional-ask-a-question-about-a-document)),
then search a set of course notes with `knowledge.search`
([knowledge documents](../features/knowledge.md)).

Exercises:

- For ten questions, label the document and passage that answers each, and measure
  recall@k on `knowledge.search` ([evaluate your workload](../getting-started/evaluate.md#memory-and-rag)).
- Add questions the notes do not answer and measure how often search returns nothing
  useful.

## Lab 3: a code index for a coding agent

Index a repository and connect a coding agent to it
([index a repository](../getting-started/quickstart.md#optional-index-a-repository-on-the-released-stack),
[connect a coding agent](../getting-started/connect-a-coding-agent.md)). Then ask where a
symbol is defined, who calls it, and what a change to it touches
([what the code index answers](../features/code-index/overview.md#what-it-answers)).

Exercises:

- Pick five functions and compare `code.callers` with what `grep` finds; explain each
  difference ([reading an answer](../features/code-index/overview.md#reading-an-answer)).
- Change a function on a branch and read what `code.impact` reports for the diff.

## Lab 4: memory as a database

Loom keeps its data as rows in ClickHouse tables. The instructor runs
`loom sql-access grant` once to make a read-only ClickHouse user, and students read a
namespace's memories, conversations and usage with SQL through its views
([SQL surface](../reference/sql-surface.md)).

Exercises:

- Write a query for the facts saved in the last hour, by agent.
- Compare a `memory.search` result with a SQL query over the same rows and explain what
  ranking adds.

## Which labs fit which course

| Course                | Labs    | Read next                                                                                                 |
| --------------------- | ------- | --------------------------------------------------------------------------------------------------------- |
| AI agents             | 1, 2, 3 | [Integrate Loom into an agent](agent-integration.md), [memory](../concepts/memory.md)                     |
| Information retrieval | 1, 2    | [Search and ask](../features/memory/search-and-ask.md), [evaluate Loom](../getting-started/evaluate.md)   |
| Databases             | 1, 4    | [Storage authority](../architecture/storage-authority.md), [SQL surface](../reference/sql-surface.md)     |
| Software engineering  | 3       | [Code index](../features/code-index/overview.md), [PR history](../features/code-index/history-and-prs.md) |
