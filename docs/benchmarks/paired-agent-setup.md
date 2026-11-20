# Paired coding-agent benchmark setup

A coding-agent comparison needs an isolated repository checkout, a matching code
index and an instrumented agent runner. This guide explains how to qualify that
setup before measuring tool choice, task resolution, tokens, cost and elapsed
time. Use it with the [benchmark methodology](methodology.md) and
[running instructions](running.md).

## Pair attempts by task and repeat

Use `(task_id, repeat)` as the comparison key and
`(task_id, repeat, arm)` as the unique attempt key. Every arm in a pair starts
from the same complete source commit, problem statement and public inputs.

| Arm                    | Agent tools                                         | Context policy                                        |
| ---------------------- | --------------------------------------------------- | ----------------------------------------------------- |
| Baseline               | Source read, search, edit and fixed tests           | No Loom backend, schemas, instructions or hooks       |
| Optional retrieval     | The same source tools, plus the declared Loom tools | The model chooses; zero calls is a valid outcome      |
| Orchestrated retrieval | The same source tools                               | The application retrieves at declared workflow stages |

Freeze the provider, exact model identifier, API settings, dependency images,
visible tests, hidden graders and resource limits across paired attempts.
Adding tool definitions consumes input tokens; include that overhead in the
retrieval arm's cost. Do not add unused retrieval schemas to the baseline to
make its prompt the same length.

Persist the complete randomized schedule before generation. Keep every scheduled
failure, nonuse, abstention and budget stop. Do not select only successful
attempts or compare the subset that chose retrieval against the whole baseline.
Use identical baseline A/A copies to measure variation in the runner and model.

## Keep the checkout, SDK and runner separate

The runner operates on a fresh source-only checkout for each attempt. Its working
directory can be elsewhere if every source operation is explicitly rooted in
that checkout. The checkout provides the read, search, edit and test capabilities
that both arms share.

The SDK sends scoped operations to Loom. It does not give a remote model direct
filesystem access. In an API runner, the application advertises tool schemas,
receives structured tool requests, executes them and returns their results to the
model. Installing a coding-agent plugin does not add that loop to a raw model API.
Plugin instructions, hooks and automatic context retrieval are separate treatment
choices and must be declared if included.

Keep hidden tests, reference patches, provider credentials and controller files
outside the agent's readable paths. Do not expose a shell, Git object database,
parent checkout or arbitrary SDK operation through a source tool.

Scripted replay checks the runner's control flow. It provides no measurement of
live model quality, tool adoption or savings. A working provider credential also
does not qualify the tool loop, grading path or budget enforcement.

## Prepare private indexes at the task bases

Use a private Loom installation and database for the experiment. Follow
[local development](../development/local-development.md) and
[code-index operations](../operations/code-index-operations.md); do not copy a
running deployment's credentials or migrate a database found on an occupied port.
Record the server build and exact resource names so cleanup affects only the
experiment's processes and containers.

Each historical task needs an index at its starting commit. A current-branch
index may already contain the solution. Keep separate namespaces for different
bases, disable automatic ingestion after qualification and use one viewer token
per task namespace. The runner must bind the namespace itself; the model supplies
only the arguments of the advertised retrieval operations.

For a tree-sitter-only experiment, disable the compiler-tier workers:

```dotenv
LOOM_BACKGROUND_WORKERS=off
LOOM_CODE_SCIP_APPLY_ENABLED=false
LOOM_CODE_SCIP_DELTA_ENABLED=false
LOOM_CODE_SCIP_FULL_ENABLED=false
LOOM_CODE_SCIP_PY_DELTA_ENABLED=false
LOOM_CODE_SCIP_TS_DELTA_ENABLED=false
LOOM_CODE_SCIP_GO_DELTA_ENABLED=false
LOOM_CODE_EMBED_RECOVERY_ENABLED=false
```

Configure an entry for each repository/base in `LOOM_CODE_REPOS_JSON`, with a
separate namespace, its bare mirror and the implementation path prefixes the
experiment allows. Make the pinned commit available in that mirror first.
The index phase extracts chunks, tree-sitter symbols and edges without embedding
when `--embed` is absent:

```bash
loom code backfill \
  --org "$BENCH_ORG" --namespace "$BENCH_NAMESPACE" \
  --phase index --commit "$BENCH_COMMIT" --no-fetch --concurrency 1
```

Set the `BENCH_*` shell variables to the configured task's org, namespace and
complete base commit. Do not run the embed phase for an exact-retrieval
experiment. Empty vectors in source chunks do not establish semantic-search
quality. Indexing cost and time belong in a separate setup measurement;
include them in any amortized cost comparison.

Use Linux for the bounded parser and process supervision. Importing a grammar
package is insufficient: a failed parse can leave searchable chunks with no
symbols. Check `code.index_errors`, then request a known definition through
`code.symbol` and verify that its tier is `treesitter`, its body is available,
and its path and line span match the task checkout. `code.index_status` must
identify the expected repository and full commit. Check the compiler-tier fields
as well; disabling a worker does not erase an existing compiler index.

An elided body is a context-budget outcome. An absent definition is an abstention.
Neither proves that the index is stale. Record these separately from bodies that
differ from the local source, and retain the affected task in the comparison.

## Qualify the evaluator independently

Pin dependency images by digest and record their installed package locks. Select
a runtime compatible with each historical base. If the image is not available,
rebuild and requalify it; a copied digest or earlier receipt does not qualify a
different environment. A Dockerfile base needs a repository-qualified manifest
digest, not a bare local image identifier.

Resolve source and evaluator paths absolutely before applying patches in nested
checkouts. A patch command can exit successfully without applying an out-of-scope
patch. Verify that the required test definitions exist, collect and execute.

Run these controls on each exact task image and grader path:

1. Visible tests pass on the unmodified base.
2. Hidden regression tests fail on the base, while regression-preserving tests pass.
3. The reference patch passes every selected hidden test.

Reject missing selectors, collection errors, skipped tests and ambiguous or
repeated test identifiers. Fix an incompatible runtime at its source; do not
suppress warnings or weaken assertions to qualify it.

Containers and read-only source mounts do not, by themselves, protect grading
authority. Candidate code executing inside the test interpreter can affect its
exit status or writable reports. Qualify that boundary before treating reports
as independent outcomes. An edit filter is an additional task constraint: freeze
and disclose it, and do not describe it as a sandbox for arbitrary Python.
Keep evaluator tests and configuration fixed when applying candidate source edits.

## Record the model loop and physical transports

Record the native provider response before converting tool blocks. Preserve
opaque or signed blocks when continuing the conversation. Disable automatic
retries and alternate-model fallback unless the protocol explicitly budgets them.

| Evidence           | What to retain                                                                                       |
| ------------------ | ---------------------------------------------------------------------------------------------------- |
| Attempt identity   | Pairing key, arm, source and patch hashes, provider/model, protocol digest and terminal status       |
| Model request      | Request identity, input hash, dispatch and response times, stop reason and raw usage fields          |
| Tool choice        | Provider tool-call ID, parent turn, tool name and arguments                                          |
| Tool execution     | Matching call ID, result or error, elapsed time and whether cached evidence was returned             |
| Retrieval          | Caller (model or application), operation, scope, accepted/rejected spans, abstention and request IDs |
| Physical transport | Every dispatch, response, retry, failure, auth or metadata request                                   |
| Evaluation         | Visible and hidden outcomes, grader identity, timeout and cleanup receipts                           |

Count every model-selected tool request, including one served from a local cache.
Keep that count separate from logical backend reads and physical HTTP requests.
For example, a read with one preflight scope check and one postflight scope check
uses three physical requests. A cached tool choice can use none.

Require zero physical Loom requests in baseline attempts, including failed
requests and ambient SDK traffic. A zero logical counter alone is insufficient.
Disable ambient runs and mentor requests when using the SDK in a controlled loop.
Correlate client and server records through request IDs without logging credential
headers or arbitrary response headers.

## Freeze spending and interpret the report

Record input, output, cache-read and cache-write usage using the provider's field
names. Usage belongs to model requests; a tool result's byte size is not an
independently measured per-tool token charge. A token-counting endpoint may return
an estimate; a character limit is not
a verified token bound. Qualify the bound required by the protocol, reserve cost
before dispatch, and stop when usage or pricing cannot be reconciled. Count token
counting requests and their latency separately from generation requests.

Keep model generation, retrieval provider overhead, index setup and infrastructure
costs separate. State whether prices are list-price estimates or reconciled
charges. Missing billing evidence stays unknown. Cost per successful resolution
includes failed attempts and has no finite estimate when nothing resolves.

Measure each attempt from setup through independent grading and cleanup. Report
provider, retrieval and test latency alongside that total; do not add components
that overlap. Preserve in-flight requests and reservations after a timeout.

Average repeats within tasks and resample paired task clusters for intervals.
Report A/A variation and every task category, including regressions. Missing
attempts suppress complete-cohort intervals. A small historical pilot measures
the declared workflow; it does not establish general adoption, quality or savings.
An exact-lookup tool set also does not measure the full code plane or a plugin's
behavior. Publish results only after the live path and all required receipts
qualify.
