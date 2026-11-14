# Teaching with Loom

Loom can serve as the lab environment for a course on AI agents, information retrieval,
databases or software engineering. Each student runs their own single-user stack, on a
laptop with Docker or in a GitHub codespace with nothing installed on their machine. Each
lab links the page that does its work.

## Setup

| Students have                  | Start with                                                                                  |
| ------------------------------ | ------------------------------------------------------------------------------------------- |
| A GitHub account and a browser | [Try it in GitHub Codespaces](../getting-started/quickstart.md#try-it-in-github-codespaces) |
| Docker on their own machine    | [Quickstart: fast path](../getting-started/quickstart.md#fast-path-released-image-no-clone) |

The codespace runs on a 4-core machine with 16 GB of memory, and its hours count against
each student's Codespaces quota.

The stock install needs no API key: it embeds with a bundled local model, so search works
offline, and no write verdict calls an LLM. Extraction, summaries and generated answers
(`memory.ask`, `knowledge.synthesize`) need a real LLM
([models and API keys](../getting-started/models-and-keys.md)); the exercises below use
writes, reads and search only.

Lab 1 also needs the Python SDK (Python 3.11 or newer) in a virtual environment on the
host; in a codespace, run `sudo apt-get update && sudo apt-get install -y python3-venv`
first:

```bash
python3 -m venv .venv && . .venv/bin/activate && pip install clickhouse-loom
```

The `docker compose` commands in the labs run in the host's terminal, act on the stack's
containers, and name its install directory as `<dir>`: `~/.loom` on a laptop and
`/workspaces/.loom` in a codespace.

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

Ingest a few documents with `loom demo`, which also prints the top search hits for a
question ([ask a question about a document](../getting-started/quickstart.md#optional-ask-a-question-about-a-document)).
The path is inside the container, which holds these docs under `/app/docs/`. The path also
names the document, so copy a file of your own in under its own name first:

```bash
docker compose --project-directory <dir> exec loom loom demo --file /app/docs/getting-started/quickstart.md --question "How do I get a token?"
docker compose --project-directory <dir> cp <path> loom:/tmp/<name>
docker compose --project-directory <dir> exec loom loom demo --file /tmp/<name> --question "<a question the file answers>"
```

Then search the namespace's documents with `knowledge.search` over HTTP (with no
`Authorization` header on the single-user stack) or the SDK
([knowledge documents](../features/knowledge.md)).

Exercises:

- For ten questions, label the document and passage that answers each, and measure
  recall@k on `knowledge.search` ([evaluate your workload](../getting-started/evaluate.md#memory-and-rag)).
- Add questions the documents do not answer. `knowledge.search` still returns hits for
  them, so compare the highest `similarity` among the hits (1 minus the cosine distance)
  on answerable and unanswerable questions and pick a cutoff that separates them. A hit's
  `score` is a fused rank in the default hybrid mode and does not separate them.

## Lab 3: a code index

Index a repository on the stack
([index a repository](../getting-started/quickstart.md#optional-index-a-repository-on-the-released-stack)),
then ask where a symbol is defined, who calls it, and what a change to it touches
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
namespace through its views ([SQL surface](../reference/sql-surface.md)). Make one for the
stack's namespace, `local` (the first command prints its password), then query as it:

```bash
docker compose --project-directory <dir> exec loom loom sql-access grant --user student1 --org dev --namespaces local
docker compose --project-directory <dir> exec clickhouse clickhouse-client --user student1 --password <password>
```

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
