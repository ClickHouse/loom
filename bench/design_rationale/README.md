# Design-rationale bench

Measures whether the design decisions Loom serves from a repository's design documents
keep a coding agent from re-proposing rejected options, and whether the agent answers
"why" questions correctly, cited, and without stale or invented rationale. The manual is
`python -m bench.design_rationale --help`; this page holds the arms, the task-set schema
and the outputs.

The real task set is private material and is never committed. The harness reads a task
set from the directory given on the command line; `example/` is a small synthetic one
(an invented thumbnail service) that the unit tests run end to end.

## Arms

| Arm | What the agent is shown                                                                                                                                               |
| --- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 0   | Nothing                                                                                                                                                               |
| 1   | The whole document, through `knowledge.get`                                                                                                                           |
| 2   | The document ingested as knowledge, through the knowledge hits of `loom_search`                                                                                       |
| 3   | The gold rows, through the pre-edit block (`memory.for_file` for each file the task touches) and the memory section of `loom_search`                                  |
| 4   | The same surfaces over the rows `loom decisions extract --apply` mined from the documents, or with `--arm4-namespace` the rows a document source's sync already wrote |
| 5   | The gold rows through the memory section of `loom_search` only                                                                                                        |

An extracted row names code by its names. Its files, which give it a pre-edit block,
come from the repository's code index: with `--code-index` (a Loom that indexes the
repository, its org and namespace) each name is looked up with read-only `code.symbol`
calls and a name with one defining file that is not a test or a page links the row to
that file, the rule `loom decisions extract --repo` applies. The harness's own Loom has
no code index, and an arm's org must not hold one another arm reads. Without
`--code-index` arm 4 reads no pre-edit block and is served through `loom_search` only,
and arm 5 is added so that arm 4 is compared with gold rows served the same way (the
report then leaves out arm 4 against arm 3). With `--arm4-namespace` the sync's rows
keep the files it linked, arm 4 reads the pre-edit block and is compared with arm 3.

Each role's namespace lives in an org of its own: `<org>-<prefix>-<role>`, the prefix
`dr-<hex>` fixed per run, since a search and the cost readback read a whole org. The
gold rows are never written where arm 4 extracts. A namespace given with
`--arm4-namespace` is the exception: it is read in `--org` itself, the harness neither
registers nor erases it, and its server spend is not read back (its daily cost cap, if
any, is the deployment's).

## Running

```bash
PYTHONPATH=$PWD .venv/bin/python -m bench.design_rationale run <taskset> --out <dir> --dry-run \
  --reader-model <m> --judge-model <m>
PYTHONPATH=$PWD .venv/bin/python -m bench.design_rationale run <taskset> --out <dir> \
  --base-url http://127.0.0.1:<port> --reader-model <m> --judge-model <m> \
  --code-index <url> --code-index-org <org> --code-index-namespace <ns> --max-usd <cap>
PYTHONPATH=$PWD .venv/bin/python -m bench.design_rationale score <taskset> --out <dir> \
  --extraction-labels <dir>/extraction_labels.todo.json
```

Each run creates its own orgs, so the server runs with auth off (as the dev setup starts
it), or the token covers every org. For the writes:

```bash
loom mint-token --subject <name> --org '*' --namespaces '*' --roles writer
```

Erasing the namespaces at the end needs an org-wide admin:

```bash
loom mint-token --subject sa:<name> --org '*' --platform-admin --ttl-days 1
```

Pass the admin token as `--token` for the run to erase at the end. After a writer-token
run, whose erasure is refused, rerun the same command with the admin token, or run the
`loom namespaces-gc` command it printed.

The server is a Loom server (and its ClickHouse) you started for this run, never one
found listening, with real providers and the server's environment in the shell: arm 4
runs `loom decisions extract` in a child process, on the model those settings name. The
dry run prints the calls, tokens and spend, typical and at every output cap. Each task
is answered K times per arm, each answer judged in both orders and twice per order; arms
are compared paired per task with a bootstrap over documents, and each comparison
reports its minimum detectable effect.

## Task kinds

- **A, why questions.** Scored on argument precision and recall against the gold
  arguments, a potentially-misleading flag, and citations.
- **B, change tasks** where the obvious change is a rejected option or breaks a
  constraint. The agent writes a plan and a diff; deterministic detectors read the diff
  first, a judge the rest. Scored on avoidance (no forbidden option proposed, no detector
  fired, and the rubric passed where the task has one) and design satisfaction (no
  constraint broken, no detector fired, and the rubric passed where the task has one).
- **C, supersession and abstention.** A supersession task asks about a statement a later
  revision of the document reversed: serving the stale statement as governing fails. An
  abstention task asks about something no document records: an answer that does not
  abstain, or that invents a rationale, fails.

## Directory layout

```
<taskset>/
  documents.json
  gold_rows.json
  tasks.json
  <the document files documents.json names>
```

`documents.json` is a list of documents:

| Field         | Meaning                                                                                                                                                                |
| ------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `id`          | The document's id, which tasks and gold rows name                                                                                                                      |
| `source_uri`  | The `source_uri` it is ingested under                                                                                                                                  |
| `title`       | Its title                                                                                                                                                              |
| `path`        | The markdown file, relative to the directory (or absolute)                                                                                                             |
| `revision_of` | Optional: the id of the document this one revises. It shares that document's `source_uri` and is ingested after every first version, so the earlier text is superseded |

`gold_rows.json` is a list of decision rows, each written once for arms 3 and 5:
`memory.pin` for a pinned row, `memory.set` otherwise.

| Field                                                 | Meaning                                                                                                                                              |
| ----------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------- |
| `memory_key`, `value`, `files`, `tags`, `memory_type` | As `memory.set` takes them (`memory_type` defaults to `decision`)                                                                                    |
| `document`, `section`                                 | Where the row's reasoning is written; or `source_resource_id` as `knowledge:<ns>/<source_uri>#<section>`, whose namespace is replaced with the run's |
| `pinned`                                              | Default true                                                                                                                                         |
| `after_revision`                                      | Written after the revisions are ingested: a key written again is a Revise of the earlier row                                                         |
| `image_only`                                          | Its source section carries no extractable text (counted per document in `score --extraction-labels`)                                                 |

`tasks.json` holds `"system"` (how the prompts name the code base) and the lists `"A"`,
`"B"`, `"C"`. Every task has `id` (without `/`), `document` and `gold_rows` (keys of
`gold_rows.json`; an empty list for an abstention task).

| Kind | Fields                                                                                                                                                   |
| ---- | -------------------------------------------------------------------------------------------------------------------------------------------------------- |
| A    | `question`, `gold_arguments` (list), `rejected_options` (list)                                                                                           |
| B    | `task`, `gold_constraints`, `forbidden_options`, `detector` (list; a `judge` detector carries the rubric)                                                |
| C    | `question`, `subtype` (`supersession` or `abstention`); a supersession task adds `current` (the design now) and `stale_claims` (the reversed statements) |

A detector is one of:

| `type`               | Fires when                                                      |
| -------------------- | --------------------------------------------------------------- |
| `file_touched`       | the diff modifies any of `paths`                                |
| `diff_added_regex`   | an added line in a file matching `path_glob` matches `pattern`  |
| `diff_removed_regex` | a removed line in a file matching `path_glob` matches `pattern` |
| `judge`              | never: its `rubric` goes to the judge                           |

A changed default is caught with a `diff_added_regex` whose `path_glob` names the file
and whose `pattern` matches the declaration line with a value other than the old one.

A detector that fires is a failure of the task. The paths detectors name are also the
files whose pre-edit block arms 3 and 4 read. The harness applies no patch and runs no
test, so a change's test pass rate is not measured.

## Outputs

`run --out <dir>` writes: `state.json` (namespaces, extraction reports), `surfaces.json`
(what each arm injected per task, with token counts and the keys each surface served),
`answers.json`, `judged.json` (citations with their resolution, detector results, every
judge pass), `ledger.jsonl` (every provider request and its price; the extraction leg as
an estimate charged before it runs; the server's own spend in the run's orgs read back
from `cost.summary` after each stage), `extract_<doc>.log` and
`extraction_labels.todo.json`. The server's spend is recorded after the fact, not
refused in advance: what stops the server itself is each namespace's daily cost cap, set
to `--max-usd` at registration. The extraction CLI binds no cost accumulator, so neither
its model calls nor the embeddings of its writes reach `cost.summary`; the ledger's
estimate, model calls only, is their only record. The run files are written through a
temp file and a rename; a ledger row an interrupt cut short is dropped when the ledger
opens, with a warning that the ledger undercounts by one call.

A run the spend cap stops, in its setup or later, keeps its namespaces, resumes on the
same `--out` (exit code 1), and prints the `loom namespaces-gc` command that erases them
if it is not resumed. A run that fails or is interrupted after its setup (a provider
error, a server that went away, Ctrl-C) keeps them too and prints the same command. A
resume must use the same `--org`, arms, `--arm4-namespace`, code index use, window size,
and reader, judge and extraction models, or it is refused. A setup that fails or is
interrupted other than by the cap leaves half-filled namespaces: it erases them (with
`--keep-namespaces` it keeps them and prints the command that erases them), and the run
is never resumed. A finished run erases its namespaces and is not resumed; with
`--keep-namespaces` it keeps them, and a rerun finds every stage done.

Fill each row's `verdict` (`correct`, `partly`, `wrong` or `misleading`) in
`extraction_labels.todo.json` and pass the file to `score --extraction-labels` for
extraction precision. `score --human-labels` takes a list of
`{"answer": "<task>/arm<a>/s<k>", "field": "avoids", "value": true}` and reports judge
accuracy and kappa; labels on `avoids` or `satisfies` for a change answer a detector
decided are left out, and `detector_fired` labels are never counted. `score` writes
`metrics.json` and prints aggregate tables with no task text; `score --summary <file>`
also writes the aggregates without the per-task rows or document ids, with the task set's
counts (documents, revisions, revised documents, tasks by kind and subtype, tasks with
gold rows), the run's flags with K and the judge orders and repeats, and its spend by
ledger stage (a last ledger row an interrupt cut short is left out and counted) and the
answers' cost per arm (their tokens at the current price table), the file a published
result commits. The run
directory holds the task set's content: keep it out of the repository.
