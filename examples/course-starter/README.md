# Loom labs

Four labs on [Loom](https://github.com/ClickHouse/loom), a memory and search store for AI
agents. You run your own Loom, in a GitHub codespace or on your laptop, and write small
Python programs and a SQL query against it. Each lab's text, exercises and background
are in [the course guide](https://github.com/ClickHouse/loom/blob/main/docs/guides/teaching.md).
No API key and no paid service is needed.

## Start Loom

**In a codespace:** on this repository's page, choose **Code**, then **Codespaces**, then
**Create codespace**. Setting it up takes several minutes; when the terminal says Loom
is running, the dashboard is on the forwarded port 7777 (the **Ports** tab). In each new
terminal, run `. .venv/bin/activate`.

**On a laptop with Docker:** install Loom with the
[quickstart's fast path](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/quickstart.md#fast-path-released-image-no-clone),
then, in this repository's directory, with Python 3.11 or newer:

```bash
python3 -m venv .venv && . .venv/bin/activate && pip install -r requirements.txt
```

## The labs

| Lab                                                                                                                                            | You write                                                    | Checked by         |
| ---------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------ | ------------------ |
| [1: an agent that remembers](https://github.com/ClickHouse/loom/blob/main/docs/guides/teaching.md#lab-1-an-agent-that-remembers)               | the TODOs in `labs/lab1.py`                                  | `grading/grade.py` |
| [2: document search with citations](https://github.com/ClickHouse/loom/blob/main/docs/guides/teaching.md#lab-2-document-search-with-citations) | your notes in `labs/notes.md`, the TODOs in `labs/lab2.py`   | `grading/grade.py` |
| [3: a code index](https://github.com/ClickHouse/loom/blob/main/docs/guides/teaching.md#lab-3-a-code-index)                                     | the commands you ran and what they answered, in `answers.md` | your teacher       |
| [4: memory as a database](https://github.com/ClickHouse/loom/blob/main/docs/guides/teaching.md#lab-4-memory-as-a-database)                     | the query in `labs/lab4.sql`                                 | `grading/grade.py` |

Each lab's written exercises go in `answers.md`; your teacher grades those.

## Check your work

With Loom running and the virtual environment active, from this directory:

```bash
python grading/grade.py
```

It runs your lab code against your Loom, prints `PASS` or `FAIL` for each check with the
reason, and exits non-zero while any check fails. On a laptop it finds the stack in
`~/.loom`; if you installed it elsewhere, set `LOOM_INSTALL_DIR` to that directory.

Every push runs the same checks on GitHub Actions, on a fresh Loom: open the **Actions**
tab, then the latest **Grade** run, whose summary lists each check.
