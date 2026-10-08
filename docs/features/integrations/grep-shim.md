# The grep shim

Needs Node 18 or later, a viewer token, and a repository in the
[code index](../code-index/overview.md).

The grep shim is a `grep`/`rg` wrapper that appends each hit's enclosing function, from
the code index, to the match line. It is for agents that only use shell grep, or run
where hooks do not (`claude --bare` skips the [Claude Code plugin's](./claude-code.md)
code shunt). The agent, its prompt and its harness stay unchanged.

```text
$ grep -rn "getSettingsRef().max_threads" src/Processors
src/Processors/QueryPlan/ReadFromMergeTree.cpp:1466:    ... max_threads ...   ⟵ DB::ReadFromMergeTree::selectRangesToRead
src/Processors/Executors/PipelineExecutor.cpp:298:      ... max_threads ...   ⟵ DB::PipelineExecutor::initializeExecution
loom: 2/2 hits scoped to their enclosing function via code.enclosing, index at c7a9fe7c9f, 180 ms
```

## Install

```bash
export PATH=/path/to/loom/integrations/grep-shim/bin:$PATH
export LOOM_BASE_URL=https://loom.example.com   # or ~/.loom/config.json {url, token}
export LOOM_API_KEY=<viewer token>              # or ~/.loom/token
export LOOM_CODE_NAMESPACE=code-myrepo
export LOOM_AGENT=pr-analyst                    # optional: names the caller in the ledger
```

`which grep` must point at the shim. Run the agent inside the checkout the namespace
indexes: paths are made relative to `git rev-parse --show-toplevel`. A branch mismatch
shows up as unresolved hits, not wrong ones.

The shim runs the first `grep` or `rg` on `PATH` that is not itself (`LOOM_GREP_REAL`,
`LOOM_RG_REAL` force a path).

## Guarantees

- **Every byte the real tool prints is printed, in order.** The only change is an
  optional suffix `   ⟵ <qualified name>` on match lines whose enclosing function the index
  knows, and a one-line summary on stderr.
- **The exit code is preserved** (grep: 0 hits, 1 no hits, 2 error).
- **Two shapes are annotated:** `path:line:text` (several operands, `-r` on a directory,
  `-H`) and `line:text` from exactly one file operand (`grep -n pattern file.cpp`, the
  shape an agent settles into once it knows the file; the path is the operand, so `-h` is
  fine there). Everything else passes through untouched: no `-n`, `-l`, `-L`, `-c`, `-o`,
  `-z`, `-Z`, `-b`, `-q`, `--color=always` (rg: `-N`, `--json`, `--files`, `-0`), stdin, or
  several files with `-h`.
- **A hard latency bound.** `LOOM_GREP_SHIM_TIMEOUT_MS` (default 2500). Any error, a
  timeout, a missing token, or more than 8 MB of output (`LOOM_GREP_SHIM_MAX_BYTES`) prints
  the raw output.
- **Bounded server load.** One `code.enclosing` call per 200 distinct hits, at most 5 in
  parallel; 1,000 hits are annotated and the rest print unannotated.

## The dependency-graph block

`LOOM_GREP_SHIM_GRAPH=on` (default off) appends a block after the real output:
`[Related from loom dependency graph]`, then one line per enclosing function with its
module, up to `LOOM_GREP_SHIM_GRAPH_K` (5) callers, callees and tests, and a
`(lower bound: …)` note where the graph is incomplete. Cutting from the header line on
gives the real bytes back. The block uses the same `code.enclosing` call, timeout and
fail-open rules; it is dropped past `LOOM_GREP_SHIM_GRAPH_MAX_ANCHORS` (20) functions or
when the output streamed (over 8 MB).

## Switches

| Variable                                | Default                                | Effect                                                                                  |
| --------------------------------------- | -------------------------------------- | --------------------------------------------------------------------------------------- |
| `LOOM_GREP_SHIM`                        | on                                     | `off` makes the shim a plain pass-through                                               |
| `LOOM_GREP_SHIM_QUIET`                  | unset                                  | set to drop the stderr summary line                                                     |
| `LOOM_GREP_SHIM_TIMEOUT_MS`             | 2500                                   | the latency bound                                                                       |
| `LOOM_GREP_SHIM_MAX_BYTES`              | 8 MB                                   | output past this passes through raw                                                     |
| `LOOM_GREP_SHIM_GRAPH`                  | off                                    | `on` appends the dependency-graph block                                                 |
| `LOOM_GREP_SHIM_GRAPH_K`                | 5                                      | neighbours per function (1–20)                                                          |
| `LOOM_GREP_SHIM_GRAPH_MAX_ANCHORS`      | 20                                     | functions past which the block is dropped                                               |
| `LOOM_CODE_NAMESPACE`, `LOOM_AGENT`     |                                        | the code namespace; the caller's agent name (default `grep-shim`)                       |
| `LOOM_CONFIG_FILE`, `LOOM_API_KEY_FILE` | `~/.loom/config.json`, `~/.loom/token` | where the URL, namespace and token are read from when the environment does not set them |

## Server side: `code.enclosing`

The shim is a thin client over one operation. `code.enclosing` (`POST /v1/code.enclosing`,
viewer) takes `locations: ["src/F.cpp:12", …]` (up to 200) and returns one result per
location in input order, with `symbol == ""` where the line is at file scope or the path
is not indexed. Every resolved row carries `symbol_id`.

- `include_body: true` (with `token_budget`, default 4000) puts each enclosing function's
  whole body on the row, fetched once per distinct function and filled in first-appearance
  order until the budget is spent; the rest carry a pasteable `next`
  (`code.symbol(symbol_id=…)`). The shim leaves it off.
- `neighbours: true` (with `neighbour_k`, default 5) returns the graph block's data: one
  entry per distinct enclosing function (at most 20; `anchors_capped` when more resolved)
  with `callers`, `callees` (compiler tier preferred; symbols outside the repository and
  parser guesses below 0.8 confidence dropped), `tests`, `module`, and the `epistemic` /
  `causes` envelope.

`code.symbol` also accepts `name: "src/F.cpp:12"` for a single location, and the Python SDK
exposes `agent.code.enclosing([...], include_body=True)`. See
[code index operations](../code-index/operations.md).

## Limits

- **Merged code only.** The index knows the branch it indexes; a hit in an uncommitted
  edit resolves against the indexed version of the file, or not at all.
- **Best effort.** Anything unexpected prints raw output; an unannotated line does not
  mean grep failed.
- **Same searches.** The agent still greps. An agent that can call MCP tools should use
  `code.search`, `code.callers` and `code.impact` directly.

## Related

- [Code index overview](../code-index/overview.md).
- [Claude Code plugin](./claude-code.md): the hook-based code shunt for agents that do run
  hooks.
- [Integrations overview](./index.md).
