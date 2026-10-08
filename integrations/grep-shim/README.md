# loom grep shim

A `grep` (and `rg`) for an agent's PATH that runs the **native** tool and then
names the function every hit sits in, from loom's symbol table:

```
$ grep -rn "getSettingsRef().max_threads" src/Processors
src/Processors/QueryPlan/ReadFromMergeTree.cpp:1466:    ... max_threads ...   ⟵ DB::ReadFromMergeTree::selectRangesToRead
src/Processors/Executors/PipelineExecutor.cpp:298:      ... max_threads ...   ⟵ DB::PipelineExecutor::initializeExecution
loom: 2/2 hits scoped to their enclosing function via code.enclosing, index at c7a9fe7c9f, 180 ms
```

## Why this exists

Some agents cannot be moved off shell grep: prompts do not change the habit,
and harnesses such as `claude --bare` skip the PreToolUse hooks the plugin's
code shunt lives in. What such an agent does after a grep is open files to
learn which function each hit is in. The shim answers that on the grep line
itself, so those turns never happen. The agent, its prompt and its harness
are unchanged.

## Compatibility promise

**Pass-through.** The shim runs the native tool with the same arguments,
stdin and stderr, and exits with its status. If the native tool dies from a
signal, the shim dies from the same signal, and SIGINT, SIGTERM, SIGHUP and
SIGQUIT sent to the shim reach the native tool. A reader that closes the
pipe early (`grep … | head`) ends the shim the way it ends grep. The native
tool is `LOOM_GREP_REAL` / `LOOM_RG_REAL` when set, otherwise the first
`grep` / `rg` on PATH that is not a copy of the shim. The shim passes
output through untouched, with nothing added, when:

- stdout is a terminal (the native tool keeps its colour and rg its headings),
- `LOOM_GREP_SHIM=off`,
- no token or server URL is configured,
- the output shape is not one line per hit with a line number: no `-n`
  (rg: no `-n` or `--column`), or any of `-l -L -c -o -q -z -Z -b`,
  `--color=always`, `-h` with several files, and for rg
  `-N --json --files --vimgrep --heading -p -0`.

**Annotation.** In the two shapes it annotates, `path:line:text` and
`line:text` with exactly one file operand, stdout carries every byte the
native tool printed, in order, invalid UTF-8 and binary-match notices
included. The only change is a suffix `   ⟵ <qualified name>` before the
newline of a match line whose `path` (or single operand) is a real file in
the checkout and whose enclosing function the index knows. Context
lines, separators and unresolved hits are never changed. Removing each
suffix gives back the native bytes exactly.

**Bounds.** One `code.enclosing` call per 200 distinct hits, at most 5 in
parallel (1,000 hits; the rest print unannotated), all within
`LOOM_GREP_SHIM_TIMEOUT_MS` (default 2500). A timeout, an error status,
unparseable JSON or any other failure prints the native output unannotated.
Past `LOOM_GREP_SHIM_MAX_BYTES` of output (default 8 MB) the shim stops
buffering and streams the rest as is, with backpressure.

**Graph block.** `LOOM_GREP_SHIM_GRAPH=on` (default off) appends ONE block
after the native output: `[Related from loom dependency graph]`, then one
line per enclosing function the hits resolved to, with its module, up to
`LOOM_GREP_SHIM_GRAPH_K` (5) callers, callees and mapped tests, and a
`(lower bound: …)` note when the graph is known to be incomplete for it. It
rides on the first `code.enclosing` call under the same rules. It is dropped
when more than `LOOM_GREP_SHIM_GRAPH_MAX_ANCHORS` (20) functions resolved,
when the output streamed, or when the output does not end in a newline.
Stripping everything from the header line on gives the annotated output back.

Zero dependencies; Node ≥ 18 (present wherever the claude CLI runs).

## Install

```sh
export PATH=/path/to/loom/integrations/grep-shim/bin:$PATH
export LOOM_BASE_URL=https://<your-loom>          # or ~/.loom/config.json {url, token}
export LOOM_API_KEY=<viewer token>
export LOOM_CODE_NAMESPACE=code-clickhouse   # release branches: code-clickhouse-26-8 …
export LOOM_AGENT=pr-analyst                 # optional: trace attribution
```

`which grep` must now point at the shim. `LOOM_GREP_SHIM_QUIET=1` drops the
one-line stderr summary. Paths are made repo-relative against
`git rev-parse --show-toplevel` of the working directory, so run it inside
the checkout the namespace indexes. On a branch the index does not cover,
hits in moved files stay unannotated rather than wrongly named.

## Layout

`bin/grep` and `bin/rg` are three-line wrappers that call `main()` in
`lib/shim.mjs`, the runtime (process, stream and network handling). The
parsing and annotation rules are pure functions in `lib/annotate.mjs`.

## Server side

`code.enclosing` (`POST /v1/code.enclosing`, viewer role): `locations:
["src/F.cpp:12", …]` (≤200) → one result per location, input order, `symbol
== ""` where the line is at file scope or the path is not indexed. One
symbol-table read per 20 distinct paths; `budget_p99_ms=800`. Every resolved
row carries `symbol_id`. Pass `include_body: true` (with `token_budget`,
default 4000) to get each enclosing function's whole body on the row, fetched
once per distinct function, filled in first-appearance order until the budget
is spent, the rest elided with a pasteable `next` (`code.symbol(symbol_id=…)`).
That is the one-step "grep hit → whole function" path for a CLI that consumes
the answer directly; the shim itself leaves it off. Pass `neighbours: true`
(with `neighbour_k`, default 5) for the graph block: `neighbours` holds one
entry per distinct enclosing function (≤20, `anchors_capped` when more
resolved) with `callers`, `callees` (compiler tier preferred, symbols outside
the repo and parser guesses below 0.8 confidence dropped), `tests`, `module`
and the `epistemic` / `causes` envelope; rows keep `symbol_id` as the join
key. `code.symbol` also accepts `name: "src/F.cpp:12"` for a single location.
Also available as `agent.code.enclosing([...], include_body=True)` in the
Python SDK.

## Tests

```sh
node --test integrations/grep-shim/tests/*.test.mjs
```

`annotate.test.mjs` covers the rules. `shim.test.mjs` runs the shim against
the native `grep` and `rg` found on PATH (a tool that is missing skips its
cases): byte equality with Loom unreachable, suffix-only differences with a
stand-in `code.enclosing`, and the failure cases.
