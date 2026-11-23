# Loom labs

Four labs on [Loom](https://github.com/ClickHouse/loom), a memory and search store for AI
agents. You run your own Loom, in a GitHub codespace or on your laptop, and write small
Python programs and a SQL query against it. Each lab's text, exercises and background
are in [the course guide](https://github.com/ClickHouse/loom/blob/main/docs/guides/teaching.md).
The labs need no model API key. The local laptop route needs no hosted service.

## Start Loom

**On a laptop with Docker:** install Loom with the
[quickstart's fast path](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/quickstart.md#fast-path-released-image-no-clone),
then, in this repository's directory, with Python 3.11 or newer:

```bash
python3 -m venv .venv && . .venv/bin/activate && pip install -r requirements.txt
```

Use your own machine for a route without cloud compute charges. Docker, the stack's
images and data still need local memory and disk space; check the
[setup requirements](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/quickstart.md#prerequisites)
before starting.

**In a codespace (optional):** check your
[compute and storage billing](https://docs.github.com/en/billing/concepts/product-billing/github-codespaces)
first. A codespace uses its owner's quota and can incur charges; the lab does not grant
an allowance. On this repository's page, choose **Code**, then **Codespaces**, then
**Create codespace**. Setting it up takes several minutes; when the terminal says Loom
is running, the dashboard is on the forwarded port 7777 (the **Ports** tab). In each new
terminal, run `. .venv/bin/activate`.

## The labs

| Lab                                                                                                                                            | You write                                                    | Checked by         |
| ---------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------ | ------------------ |
| [1: an agent that remembers](https://github.com/ClickHouse/loom/blob/main/docs/guides/teaching.md#lab-1-an-agent-that-remembers)               | the TODOs in `labs/lab1.py`                                  | `grading/grade.py` |
| [2: document search with citations](https://github.com/ClickHouse/loom/blob/main/docs/guides/teaching.md#lab-2-document-search-with-citations) | your notes in `labs/notes.md`, the TODOs in `labs/lab2.py`   | `grading/grade.py` |
| [3: a code index](https://github.com/ClickHouse/loom/blob/main/docs/guides/teaching.md#lab-3-a-code-index)                                     | the commands you ran and what they answered, in `answers.md` | your teacher       |
| [4: memory as a database](https://github.com/ClickHouse/loom/blob/main/docs/guides/teaching.md#lab-4-memory-as-a-database)                     | the query in `labs/lab4.sql`                                 | `grading/grade.py` |

Each lab's written exercises go in `answers.md`; your teacher grades those.

For a bounded Lab 2 extension, use the [retrieval worksheet](retrieval/README.md).
It includes synthetic notes, development and evaluation labels, and a rubric for
passage recall and similarity-cutoff mistakes.

## Check your work

With Loom running and the virtual environment active, from this directory:

```bash
python grading/grade.py
```

It runs your lab code against your Loom, prints `PASS` or `FAIL` for each check with the
reason, and exits non-zero while any check fails. On a laptop it finds the stack in
`~/.loom`; if you installed it elsewhere, set `LOOM_INSTALL_DIR` to that directory.

A student step that is still waiting after 60 seconds fails that check (the line names
the check and the budget), and the later checks still run. Set `LOOM_GRADE_STEP_TIMEOUT`
to another positive number of seconds to change the budget. The deadline applies to a
cooperative async wait, such as waiting on an event or a future. It does not stop
blocking synchronous code, a CPU loop, or a coroutine that ignores cancellation.

Every push runs the same checks on GitHub Actions, on a fresh Loom: open the **Actions**
tab, then the latest **Grade** run, whose summary lists each check.

Standard hosted runner compute for a public repository is free under
[Actions billing](https://docs.github.com/en/billing/concepts/product-billing/github-actions).
Private repository compute and artifact or cache storage have separate allowances. You
can run the grader locally without enabling hosted grading; choose repository visibility
for your work's privacy, not to publish private answers or data for free compute.
