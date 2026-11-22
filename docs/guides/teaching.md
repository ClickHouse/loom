# Teaching with Loom

Loom can serve as the lab environment for a course on AI agents, information retrieval,
databases or software engineering. Each student runs their own single-user stack, on a
laptop with Docker. A hosted codespace is optional and has its own billing. Each lab
links the page that does its work.

## Setup

| Students have                  | Start with                                                                                                                |
| ------------------------------ | ------------------------------------------------------------------------------------------------------------------------- |
| Docker on their own machine    | [Quickstart: fast path](../getting-started/quickstart.md#fast-path-released-image-no-clone)                               |
| A GitHub account and a browser | [Try it in GitHub Codespaces](../getting-started/quickstart.md#try-it-in-github-codespaces), after checking billing below |

A codespace uses the student's Codespaces quota while it runs and while its disk is kept, and
GitHub deletes one left unused for its retention period, with its data
([billing](https://docs.github.com/en/billing/concepts/product-billing/github-codespaces),
[automatic deletion](https://docs.github.com/en/codespaces/setting-your-user-preferences/configuring-automatic-deletion-of-your-codespaces)).
The dev container's `hostRequirements` set the smallest machine it runs on
([`.devcontainer/devcontainer.json`](../../.devcontainer/devcontainer.json)).

The stock install needs no API key: it embeds with a bundled local model, so search works
offline, and no write verdict calls an LLM. Extraction, summaries and generated answers
(`memory.ask`, `knowledge.synthesize`) need a real LLM
([models and API keys](../getting-started/models-and-keys.md)); the exercises below use
writes, reads and search only.

Lab 1, and the SDK route in Lab 2, need the Python SDK in a virtual environment, in a
terminal on the laptop or in the codespace. On Debian or Ubuntu, a codespace and WSL
included, run `sudo apt-get update && sudo apt-get install -y python3-venv` before the
commands below. The SDK needs Python 3.11 or newer; `python3 --version` says which one is
installed. A student without Python 3.11 or newer can use the codespace, where this step
installs a new enough Python.

```bash
python3 -m venv .venv && . .venv/bin/activate && pip install clickhouse-loom
```

The `docker compose` commands in the labs run in that same terminal, act on the stack's
containers, and name its install directory as `<dir>`: `~/.loom` on a laptop and
`/workspaces/.loom` in a codespace.

## Hand out the labs as an assignment

The [course starter](../../examples/course-starter/README.md) is a repository for each
student: the code of Labs 1 and 2 and the Lab 4 query, with their gaps marked `TODO`, an
answers page for the written exercises, a dev container that installs the latest release
in a codespace with the Python SDK and Claude Code, and a grading workflow. Copy it into a
new repository under your account or your school's organization, and mark that
repository as a template (it needs `curl`, `tar`, `git` and the
[GitHub CLI](https://cli.github.com/), signed in):

```bash
mkdir loom-labs && cd loom-labs
curl -fsSL https://github.com/ClickHouse/loom/archive/refs/heads/main.tar.gz \
  | tar -xz --strip-components=3 loom-main/examples/course-starter
git init -b main && git add . && git commit -m "Loom labs"
gh repo create <owner>/loom-labs --public --source . --push
gh repo edit <owner>/loom-labs --template
```

The push runs the grading workflow once, before the repository is a template, and that run
fails by design: each check's line names the part of the starter still to be written. The
copy is the starter on `main`, tested against `main`, while every run installs the latest
release, so a failure for any other reason means the two differ: copy the starter again
after the next release. Later pushes to the template are not graded.

Each student then works in a repository made from that template, with **Use this
template** on its page or with the tool that hands out your assignments, and opens it in
a codespace or clones it to a laptop. A student needs read access to the template to use
it: a public one holds no answers, and a private one works for the members of its
organization or the collaborators you add.

`python grading/grade.py` runs the lab code against the student's running stack and checks
what it wrote and returned, not how it got there, so read the code too. The `Grade` workflow
runs the same checks on every push, against a fresh stack on a GitHub Actions runner, and
writes the table of checks to the run's summary; the run fails while any check fails. Each
check fails on the untouched starter and needs no API key. Lab 3 and the answers page are
graded by hand: their exercises ask for explanations.

A grading run takes about two minutes, most of it starting the stack. In a private
repository those minutes count against the Actions quota of the account or organization
that owns it ([Actions billing](https://docs.github.com/en/billing/concepts/product-billing/github-actions));
codespaces cost what [Setup](#setup) says. Standard hosted compute in public repositories
is free under those terms; artifact and cache storage have separate allowances. Local
grading avoids hosted compute charges. Keep student submissions, grades and personal
data private when deciding how to distribute and grade the work.

## Lab 1: an agent that remembers

Build a small agent with the Python SDK: an identity, facts it saves and reads back, a
session, and a run. Follow [Your first agent](../getting-started/first-agent.md).

Exercises:

- Search with `memory.search` for something no saved fact answers, and explain the
  `abstain_code` the reply carries
  ([search and ask](../features/memory/search-and-ask.md)).
- Save a fact, then a close but different value under another key, and read the write
  verdict ([how a write is judged](../features/memory/conflicts-and-refusal.md#how-a-write-is-judged)).
- Revise a fact and read its versions with `memory.history`
  ([history and provenance](../features/memory/history-and-provenance.md#versions-memoryhistory)).

## Lab 2: document search with citations

Ingest a few documents with `loom demo`, which on the stock stack (fake LLM) prints the
top `knowledge.search` hits for a question instead of an answer ([ask a question about a
document](../getting-started/quickstart.md#optional-ask-a-question-about-a-document)). The
`--file` path is inside the container, which holds these docs under `/app/docs/`. Copy a
file of your own in first (here `notes.md` from the current directory), under a name that
identifies it, since `loom demo` names the document by its path: a second file copied to
the same path replaces the first as its next revision.

```bash
docker compose --project-directory <dir> exec loom loom demo --file /app/docs/getting-started/quickstart.md --question "How do I get a token?"
docker compose --project-directory <dir> exec -T loom sh -c 'cat > /tmp/notes.md' < ./notes.md
docker compose --project-directory <dir> exec loom loom demo --file /tmp/notes.md --question "<a question the file answers>"
```

Then search the namespace's documents with `knowledge.search` over HTTP (with no
`Authorization` header on the single-user stack) or the SDK
([knowledge documents](../features/knowledge.md)). The SDK's `knowledge.ingest_file`
publishes a local file without copying it into the container; the course starter's
`labs/lab2.py` asks for it.

Exercises:

- For ten questions, label the document and passage that answers each, and measure
  recall@k on `knowledge.search` ([evaluate your workload](../getting-started/evaluate.md#memory-and-rag)).
- Add questions the documents do not answer. `knowledge.search` still returns hits for
  them, so compare the highest `similarity` among the hits (1 minus the cosine distance)
  on answerable and unanswerable questions and pick a cutoff that separates them. A hit's
  `score` is a fused rank in the default hybrid mode and does not separate them.

The [course starter retrieval worksheet](../../examples/course-starter/retrieval/README.md)
supplies a synthetic document, labeled practice questions and an assessment rubric.
Its public evaluation labels teach the procedure; an instructor uses different
private questions for an assessment.

## Lab 3: a code index

Index a repository on the stack
([index a repository](../getting-started/quickstart.md#optional-index-a-repository-on-the-released-stack)).
Then ask where a symbol is defined, who calls it, and what a change to it touches ([what
the code index answers](../features/code-index/overview.md#what-it-answers)) over HTTP,
with `POST /v1/code.<op>`. The single-user stack does not fill in `code.*` calls, so each
body names `"org": "dev"` and the repository's `"namespace"` (`code-myrepo` in the
quickstart's entry).

A coding agent can ask the same questions once connected
([connect a coding agent](../getting-started/connect-a-coding-agent.md)). It runs on its own
provider's model, so it needs a sign-in or API key there; the stack does not supply one.

Exercises:

- Pick five functions and compare `code.callers` with what `grep` finds; explain each
  difference ([reading an answer](../features/code-index/overview.md#reading-an-answer)).
- In a clone of the indexed repository, change a function and send the diff to
  `code.impact` as `diff`
  ([how an agent uses it](../features/code-index/overview.md#how-an-agent-uses-it)). The
  command needs `jq` (`sudo apt-get install -y jq` on Debian or Ubuntu):

  ```bash
  git diff | jq -Rs '{org: "dev", namespace: "code-myrepo", diff: .}' \
    | curl -s -X POST http://localhost:7777/v1/code.impact -H 'Content-Type: application/json' -d @-
  ```

## Lab 4: memory as a database

Loom keeps its data as rows in ClickHouse tables, and a read-only ClickHouse user reads a
namespace through its views ([SQL surface](../reference/sql-surface.md)). Make one for the
stack's namespace, `local` (the first command prints its password), then query as it:

```bash
docker compose --project-directory <dir> exec loom loom sql-access grant --user student1 --org dev --namespaces local
docker compose --project-directory <dir> exec clickhouse clickhouse-client --user student1 --password <password>
```

Exercises:

- Write a query over `loom.sql_v1_memories` that counts the facts each agent created in
  the last hour (by `created_at`, which a revision keeps).
- Compare a `memory.search` result with a SQL query over the same rows and explain what
  ranking adds.

## Which labs fit which course

| Course                | Labs    | Read next                                                                                                 |
| --------------------- | ------- | --------------------------------------------------------------------------------------------------------- |
| AI agents             | 1, 2, 3 | [Integrate Loom into an agent](agent-integration.md), [memory](../concepts/memory.md)                     |
| Information retrieval | 1, 2    | [Search and ask](../features/memory/search-and-ask.md), [evaluate Loom](../getting-started/evaluate.md)   |
| Databases             | 1, 4    | [Storage authority](../architecture/storage-authority.md), [SQL surface](../reference/sql-surface.md)     |
| Software engineering  | 3       | [Code index](../features/code-index/overview.md), [PR history](../features/code-index/history-and-prs.md) |

## Bounded projects after the labs

Use the existing labs as the starting point. Each project produces a small reproducible
artifact and an explanation of its limits, without generated answers or paid model calls.

| Project                             | Deliverable                                                                                                                    | Acceptance                                                                                                                                                                   |
| ----------------------------------- | ------------------------------------------------------------------------------------------------------------------------------ | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Retrieval evaluation from Lab 2     | A synthetic document, answerable questions with labeled passages, unanswerable questions and the search script.                | Separate cutoff selection from held-out questions. Report recall@k and false positives and false negatives for the chosen similarity cutoff; retain the raw hits and labels. |
| Call graph investigation from Lab 3 | A small repository fixture you may redistribute, expected caller relationships and commands for both index and literal search. | Explain each missed or extra relationship, including dynamic calls; do not present static results as a complete runtime graph.                                               |
| Memory history from Lab 1           | A script that writes, revises and reads one synthetic fact and its versions.                                                   | Show the before/after values and provenance, then repeat on a fresh namespace without affecting another student's data.                                                      |

An instructor can assess reproducibility, correctness of the labels or expected results,
and the explanation of failure cases separately. Preserve the repository commit, released
image digest, SDK version, configuration, commands and raw results with the submission.
License original fixtures explicitly; preserve the license and notices for reused material.
Use synthetic data rather than student records or private documents. A local retrieval
exercise does not establish general answer quality or a published benchmark result.

A student who finds a reproducible defect can follow the
[first contribution path](../../CONTRIBUTING.md#choose-a-first-contribution). Search for an
existing issue and active work first; a course project does not reserve upstream work or
promise acceptance. Use the repository's [software citation metadata](../../CITATION.cff)
and record the exact source commit or release used in a report.
