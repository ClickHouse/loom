# Analyzer statement corpus

Loom's ClickHouse statements are Python strings built by ~200 builders under
hundreds of flags. Unit stubs accept any string, so an alias that shadows a
filtered column (`argMax(agent, ts) AS agent`), a CTE read in a statement
without the `WITH`, an outer alias used inside a subquery, or a parameter
type the driver cannot bind reaches staging as a leg that *fails open* —
missing rows, not an error. The analyzer needs no rows: `EXPLAIN QUERY TREE`
against the migrated (empty) CI tables rejects every one of those in a few
milliseconds.

This directory drives the **builders**, not the constants, the way
BuzzHouse drives ClickHouse from a grammar: the value is in the generator.

```
corpus.py            harness: RecordingCH stub, Case/axes product, digest dedup,
                     typed dummy params, EXPLAIN QUERY TREE, capture import, report
cases/               one module per plane; each exposes cases() -> list[Case]
  search.py          vector_search: modes x scope x key axis, filters, rank terms,
                     fact leg two-stage/re-read/bare-ranking, windows, planes
  legs.py            chunk leg stages, lexical union, key-match arm, graph/summary
                     fetches, memory store readers/finders/worker scans, deep evidence
  code.py            CodeRepo: search legs, edges, PR readers, point readers
  erasure.py         every saga step (namespace / list / user, tombstone / purge,
                     lightweight / rewrite), census, exports, ledger reads
  planes.py          handlers through their Input models (`filled` axis fills every
                     optional field with a typed dummy) + the plane repositories
  workers.py         pure *_sql builders and constants, recall builder, worker passes,
                     admin readers, boot reads
  tracker.py         tracker links, daily roll-up, backport report and record-vector readers
shapes.txt           committed shape list: <shape12>\t<200-char head, literals masked>
test_corpus.py       the tests CI runs
```

## How a statement gets checked

1. Each `Case` names a builder entry point, its axes (a settings field name is
   applied to `settings` for the drive; every axis value reaches the drive
   in the combination dict) and an optional `rows(sql, params)` function that
   scripts the stub's answers so a builder proceeds to its second statement.
   The axis product is capped at 64 per case — split the case, not the cap.
2. Every statement the drive issues is recorded with the builder's own
   parameters. Its **shape** is the statement digest (string and numeric
   literals masked): two searches with different vectors or namespaces are
   one shape; a different SELECT list, join or predicate is a new one.
3. `check()` hands each unique shape to `EXPLAIN QUERY TREE` with typed
   parameters (the builder's where they fit the placeholder type, a dummy
   otherwise). `INSERT … SELECT` contributes its SELECT; `ALTER … UPDATE a = x
   WHERE p` becomes `SELECT (x) AS a FROM t WHERE p` and a DELETE its
   predicate as `SELECT count()`; DDL, `SYSTEM`, `OPTIMIZE` and VALUES inserts
   are listed as unchecked. A vector-ranked read is rendered with the
   codes flag on — `render_all` pins both the server-side answer and the
   deployment flag `vector_search_quantized_codes`, so a `.env` that turns
   the flag off renders the same set as the CLI and CI (the off form is
   carried by the `vector_search.fact_leg_codes_first` axis); the check
   strips the flag on a server that does not know the setting, as
   `server/core/ch.py` does at run time.
4. `shapes.txt` is the review surface: `test_shapes_file_matches_builders`
   fails when the rendered set differs from the file, so a PR that changes a
   statement's shape shows the new head in its diff.

Three more tests keep the case set honest, and none of them needs ClickHouse:

- `test_no_drive_calls_a_missing_server_symbol`: a drive that ends on a
  missing module attribute, an import error or a call-signature `TypeError`
  stopped at a function that moved, and every statement after that call
  would leave `shapes.txt` as "gone".
- `test_every_axis_moves_a_shape`: an axis whose values all render the same
  shapes drives a value the builder does not branch on. Drive a real value
  (the op's `Literal`) or pin the axis to one value.
- `test_every_case_module_is_registered` and `test_shapes_file_is_in_written_form`:
  a module missing from `cases.MODULES` renders nothing, and a hand-edited or
  mis-merged `shapes.txt` is not what `--write` produces.

## Running

```
# a migrated ClickHouse: the CI integration service, or a throwaway container
docker run -d --pull always --name corpus-ch -p 127.0.0.1:18160:8123 -e CLICKHOUSE_DB=loom \
  -e CLICKHOUSE_USER=loom -e CLICKHOUSE_PASSWORD=loomdev \
  -e CLICKHOUSE_DEFAULT_ACCESS_MANAGEMENT=1 \
  -v $PWD/deploy/lemmagen3:/var/lib/clickhouse/lemmagen3:ro \
  -v $PWD/deploy/clickhouse-config/lemmatizers.xml:/etc/clickhouse-server/config.d/lemmatizers.xml:ro \
  -v $PWD/deploy/clickhouse-config/nlp_user_settings.xml:/etc/clickhouse-server/users.d/nlp_user_settings.xml:ro \
  -v $PWD/deploy/clickhouse-config/keeper.xml:/etc/clickhouse-server/config.d/keeper.xml:ro \
  clickhouse/clickhouse-server:latest
export LOOM_CH_HOST=127.0.0.1 LOOM_CH_PORT=18160 LOOM_CH_USER=loom LOOM_CH_PASSWORD=loomdev \
       LOOM_CH_DATABASE=loom LOOM_CH_SECURE=false
python -m server.core.schema.migrate

pytest tests/analyzer -q                              # what CI runs
python -m tests.analyzer.corpus --report              # coverage table, no ClickHouse
python -m tests.analyzer.corpus --check               # every shape through the analyzer
python -m tests.analyzer.corpus --write               # regenerate shapes.txt (review the diff)
python -m tests.analyzer.corpus --case erasure --errors   # one plane, with drive errors
```

The check needs the target *migrated*: the analyzer resolves names against
the target's tables, so a database whose `loom._migrations` ledger lacks repo
files rejects every shape that reads what those files add (code 60 unknown
table, code 47 unknown identifier) — the target is behind, not the statement.
`--check` prints a warning naming the pending files; the pytest test skips
with the same reason (and fails under `CI`, where `python -m server.core.schema.migrate`
runs first). The local dev container at `localhost:8123`, the default when
no `LOOM_CH_*` is set, is migrated only by a server boot or `make migrate`.

`--errors` prints, per combination, the exception a drive ended with. A
builder that stops after its first statement (a `NotFound` on an empty
stub) still rendered that statement; a drive that renders *nothing* fails
`test_every_case_renders_a_statement` — that is a broken case, not a
covered builder.

## Production shapes

`LOOM_CH_CAPTURE_STATEMENTS=<path>` on any loom process appends one JSON
line per executed statement (`digest`, `sql`) from `server/core/ch.py`. Run the
acceptance benches against an isolated server with it set, then

```
python -m tests.analyzer.corpus --check --import /path/to/statements.jsonl
```

checks the shapes production actually executed alongside the generated
ones (parameters are synthesised from the `{name:Type}` placeholders).

## Adding a case

Add an `async def drive(ch, combo)` and a `Case(...)` to the plane's module
(or a new module listed in `cases/__init__.py`). Import the server modules
the drive needs at module scope or inside the drive — but a *handler* module
must be imported before any drive runs (see `planes._handler_drive`), or its
`from server.core.ch import get_client` binds the stub. Then `--write`, review
the new lines in `shapes.txt`, and commit both.

## What is not here

- `server/cli/import_*.py`'s SQL runs against the *source* app's SQLite, not
  ClickHouse; `transport/mcp_descriptions.py` carries SQL in tool
  descriptions; the extraction prompt constants
  (`server/memory/domain/extraction_prompts.py`) are prompt text. They count
  in a constant census and are not statements.
- Handlers that call an LLM or an embedder (set, ask, consolidate, knowledge
  add) are not driven; their statements come through the repositories.
- DDL and `SYSTEM`/`OPTIMIZE` statements are listed as unchecked.
