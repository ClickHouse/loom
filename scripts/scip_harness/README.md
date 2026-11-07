# SCIP harness — compiler-tier deltas for the loom code plane

Produces the `tier='scip'` (compiler-exact) symbols and call edges by
running [scip-clang](https://github.com/sourcegraph/scip-clang) over the
**changed translation units** of one commit range, converting the protobuf
index to loom's versioned JSON delta, and uploading it chunked through
`POST /v1/code.scip_ingest`. The `code_scip_apply` worker assembles and
applies complete batches idempotently.

Run it per merge or nightly. The C/C++ harness configures CMake (compile
database only) when `build/compile_commands.json` or `build/CMakeCache.txt` is
missing, and reconfigures on the existing `build/` cache before a full index and
for a window that adds a translation unit or edits a `CMakeLists.txt` or `.cmake`
file; a configure that fails fails the run. A CI job that builds with clang can
reuse its build tree only when it configured this checkout's own `build/`
directory: CMake refuses a cache made in another directory.

## Contract

Every tier runs the same five steps; only the first is language-specific.

| Step    | What                                                                                                         | Where                                                                                                     |
| ------- | ------------------------------------------------------------------------------------------------------------ | --------------------------------------------------------------------------------------------------------- |
| window  | `<base>..<head>` of the source checkout: changed files, and deleted ones (a rename's old path included)      | `lib.sh` `window_files`                                                                                   |
| index   | the tier's indexer writes one SCIP file; a window of only deletions skips it (an empty index)                | `run_delta.sh` (scip-clang), `run_python_delta.sh`, `run_typescript_delta.sh`, `run_go_monorepo_delta.sh` |
| convert | SCIP file to loom's JSON delta, documents sorted by path                                                     | `scip_to_delta.py`                                                                                        |
| deliver | `DELTA_OUT=<file>` (`DELTA_OUT_DIR` for Go) writes the delta; otherwise it is uploaded to `code.scip_ingest` | `lib.sh` `convert_delta`                                                                                  |
| apply   | the worker (or `code_scip_apply` for an upload) applies the delta and advances the tier's watermark          | `server/code/workers/scip_*.py`, `server/code/app/index/scip_delta.py`                                    |

The in-server workers call the scripts by path with
`<checkout> <base|--full> <head>` and `DELTA_OUT` / `DELTA_OUT_DIR` set. Exit
0 with no delta file means the window has nothing for the tier. Exit 3
(`EXIT_BASE_NOT_FOUND` in `lib.sh`) means base not found: the caller should
rerun as a full index; no delta is written. Any other exit is a failed run. The converter prints one `delta: N files, …` summary
line, then `wrote <file> (sha …)`.

## Requirements

- a ClickHouse checkout that `cmake -G Ninja` can configure (the harness
  only configures — no build)
- `scip-clang` release binary on PATH (or `SCIP_CLANG=/path`); budget
  ~2–4 GB RAM per parallel job
- `git`, `python3` with `pip install 'protobuf>=7.35.1' httpx`
  (harness-only deps — deliberately NOT loom server deps)
- a loom WRITER token for the code namespace in `LOOM_API_KEY`

## Per-merge delta

```bash
LOOM_API_KEY=... scripts/scip_harness/run_delta.sh \
    ~/ClickHouse <base_sha> <head_sha> https://loom.example
```

Changed-TU selection: changed `.cpp` files, plus every TU that includes a
changed header (`git grep` for its file name, capped at `MAX_TUS=200` — above the cap it
degrades to the changed `.cpp` only and the skipped header fan-out heals on
the next `--full` run). The delta carries the window's files and every TU
selected, so an includer of a changed header gets its edges refreshed.

## Full backfill (the scip base index)

```bash
LOOM_API_KEY=... scripts/scip_harness/run_delta.sh ~/ClickHouse --full "" https://loom.example
```

Hours on a 16-core/64 GB box; uploads in ≤8 MB parts; `code_scip_apply`
drains them at its own pace. Progress: `loom code stats` or
`POST /v1/code.scip_status {"batch_id": ...}`.

## Constructor calls through factories

`std::make_shared<T>(...)` / `std::make_unique<T>(...)` call `T`'s
constructor inside the library template, so scip-clang records only a type
reference at the template argument. The converter reads the source line
under that reference (`--source-root`, default: the index's `project_root`,
i.e. the checkout the harness ran in) and emits a `calls` edge to each known
constructor overload of `T`. A `--full` run claims the `ctor_calls` feature
in the delta; the server records it in `code_index_state` and `code.callers`
stops labelling a constructor's empty answer `ctor_edges_absent` for that
namespace. Per-merge deltas emit the edges for their files but make no
namespace-wide claim — run `--full` once to make it.

## Go

`run_go_monorepo_delta.sh` runs [scip-go](https://github.com/scip-code/scip-go)
once per `go.mod` directory (or the `MODULES` list a merge touched) and converts
each through the same `scip_to_delta.py`. scip-go's symbols
(`scip-go gomod <module> <version> <pkg>/<Name>#|().`) parse through the
generic descriptor grammar, no scheme branch: `<module>/<pkg>::<Name>`
functions, `class` structs, `calls` / `inherits` / `overrides` edges. The
parser tier (lang_go) folds with them by path, bare name and line.

A single-module repository is the case of one `go.mod` at the root: that
module's directory is `.`, its path prefix is empty, and in `DELTA_OUT_DIR`
mode its delta is written to `.json`. A module below the root is converted
with `--path-prefix <dir>`, so the delta's paths are repository-relative like
the parser tier's and the two tiers fold.

```bash
go install github.com/scip-code/scip-go/cmd/scip-go@v0.2.7   # the image's version; the module moved from sourcegraph/
DELTA_OUT_DIR=/tmp/deltas JOBS=4 scripts/scip_harness/run_go_monorepo_delta.sh <checkout> --full $(git -C <checkout> rev-parse HEAD)
LOOM_API_KEY=<writer> JOBS=4 scripts/scip_harness/run_go_monorepo_delta.sh <checkout> http://127.0.0.1:7777 <org> <namespace>
```

With a base commit instead of `--full` each module's delta carries only the
Go files the window changed or deleted, and a module the window did not touch
is skipped: the shape the in-server `code_scip_go_delta` worker runs per merge
(settings `code_scip_go_*`). `JOBS` runs modules in parallel; a module whose
`go mod download` fails is skipped and logged; generated documents are dropped
by default (`EXCLUDE_GENERATED=0` keeps them).

Measured on sqlc (116 packages, 3,447 Go files): 2 min, a 12 MB index,
7,457 symbols and 11,538 edges. On a 238-module monorepo, a
425-file module: 5,461 symbols, 37,909 edges, 5 min. The apply worker
applies batches for REGISTERED namespaces only; a bench namespace registered
by `LOOM_CODE_REPOS_JSON` for the CLI runs the pass once from the worktree.

## Conversion only (no upload)

```bash
python3 scripts/scip_harness/scip_to_delta.py \
    --index index.scip --commit $sha --dry-run delta.json
```

## Failure model

- missing parts → batch stays `pending` (parts TTL 7 days); re-upload the
  same batch_id idempotently
- apply failure → batch `failed` with the error; `loom code scip-retry <batch_id>`
  after fixing the cause
- the fast (tree-sitter) tier keeps serving throughout — scip rows only
  ever _upgrade_ answers

## Getting scip-clang

On linux-x86_64, use the v0.4.0 release binary and check it against its
published digest:

```bash
curl -fsSL -o var/bin/scip-clang \
  https://github.com/sourcegraph/scip-clang/releases/download/v0.4.0/scip-clang-x86_64-linux
echo "06fd18c576f979a726c651594644ec4a35db4f471f2160b3f72eb89fa6001784  var/bin/scip-clang" | sha256sum -c -
chmod +x var/bin/scip-clang
```

## Building scip-clang on linux-arm64

Upstream publishes no linux-arm64 binary (sourcegraph/scip-clang#542); on
aarch64 hosts build from source — verified on
scip-clang 0.4.0, ~6 min on 32 cores:

```bash
sudo apt-get install -y g++ zlib1g-dev libzstd-dev   # bazel bootstraps the rest
git clone --branch v0.4.0 https://github.com/sourcegraph/scip-clang && cd scip-clang
git rev-parse HEAD   # 1860adf8deab7870fed2199eadc190e3965a92ff
bazel build //indexer:scip-clang --config=release
cp bazel-bin/indexer/scip-clang <loom checkout>/var/bin/scip-clang
```

ClickHouse checkout prerequisites for a full index: clang >= 21
(apt.llvm.org), then a configure-only cmake for compile_commands.json:
`cmake -DCOMPILER_CACHE=disabled -DENABLE_RUST=0 ...` (no build needed).

## TypeScript

`run_typescript_delta.sh <checkout> <loom_url> [org] [namespace]` runs
[scip-typescript](https://github.com/sourcegraph/scip-typescript) over a
workspace (`--pnpm-workspaces` / `--yarn-workspaces` inferred), converts
the index with the same `scip_to_delta.py` (the `scip-typescript` scheme
is detected from the first symbol) and uploads under producer
`scip-typescript`; the watermark is `scip_commit_ts`.

What the converter makes of a scip-typescript symbol
(`scip-typescript npm @langfuse/shared 1.0.0 src/server/repositories/`scores.ts`/getScoreById.`):

- the package name maps to its workspace directory through the checkout's
  `package.json` files (`--source-root`), so the qualified name is the
  tree-sitter tier's module-scoped name
  (`packages.shared.src.server.repositories.scores::getScoreById`) and
  reads merge the tiers by qualified name;
- `Name#` is a class / interface / type alias / enum (the keyword on the
  definition's line decides), `f().` a function or method, `x.` a
  const — or a function when its definition has a body span (a const-bound
  arrow); `Class#`<constructor>`().` is `Class::constructor`; parameters,
  type parameters, object-type members (`x0:`), externals and locals are
  skipped;
- a reference to a function is a `calls` edge from the innermost enclosing
  definition (spans only — never the nearest-preceding fallback scip-clang
  needs); a reference to a type is a `uses_type` edge; `is_implementation`
  relationships are `inherits` (types, interfaces included) / `overrides`
  (methods).

Requirements: node 22 or 24, `npm i -g @sourcegraph/scip-typescript@0.4.0`, the
checkout `install`ed (`pnpm install --frozen-lockfile --ignore-scripts` is
enough), `pnpm` on PATH for a pnpm workspace. **An unbuilt monorepo loses
every cross-package reference**: a package's `exports` table points at its
`dist/`, which does not exist, so `import { f } from "@langfuse/shared"`
resolves to nothing. Run `scip_ts_paths.py <checkout>` first — it derives
`compilerOptions.paths` from the workspace packages' `exports` (dist → src)
into each consumer's `tsconfig.json` (backups beside them; `--restore`
puts them back) — or build the packages. Measured on langfuse: 4,603 files,
~4 min, a 384 MB index.
