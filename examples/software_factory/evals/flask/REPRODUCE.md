# Reproduce the Flask factory comparison

This route creates a Harbor study from public inputs and a Loom deployment you
control. It compares Claude with ordinary source tools against the same Claude
loop with optional Loom tools backed by Tree-sitter or Python SCIP. Model outputs, latency and cost can vary between
runs; the protocol freezes the inputs and accounting rules.

## Inputs and requirements

Use Docker with Compose v2, Git, `uv`, Python 3.12 and a checkout containing this
factory. Record `git rev-parse HEAD` for both the controller and Loom server.
The controller needs local Docker access to inspect the serving Loom container
and audit its index. An endpoint without that access cannot satisfy this
protocol. Keep the deployment isolated from other work and keep enough Docker
memory and disk space for the test images and trial artifacts.

[suite.lock.json](suite.lock.json) pins:

- Harbor 0.27.0 through [Harbor requirements](../../harbor/requirements.txt), and
  PyArrow 26.0.1 through [bootstrap-requirements.txt](bootstrap-requirements.txt).
- The public [SWE-bench test dataset](https://huggingface.co/datasets/princeton-nlp/SWE-bench/tree/e48e2bd1e9fecd5bbd641e9414ac59da9f2e69f6),
  revision `e48e2bd1e9fecd5bbd641e9414ac59da9f2e69f6`, parquet SHA256
  `db4f70ef735b3162c74801ddcdf8d7bae8d704193788c6d844f898c20b571cbb`.
- The three complete task records, exact Flask bases, public issue statements,
  visible pass-to-pass subset and independent fail-to-pass/pass-to-pass selectors.
- Python 3.11.18 for Flask test containers by immutable base-image digest, every
  runtime and build dependency version, and published package artifact hashes.
  The controller runs Python 3.12. Record image architecture and host hardware;
  timings from different hardware are separate measurements. A host unable to
  execute the pinned image architecture needs Docker emulation.

| Public task           | Exact Flask base                           |
| --------------------- | ------------------------------------------ |
| `pallets__flask-4045` | `d8c37f43724cd9fb0870f77877b7c4c7e38a19e0` |
| `pallets__flask-4575` | `bd56d19b167822a9a23e2e9e2a07ccccc36baa8d` |
| `pallets__flask-4935` | `fa1ee7066807c21256e90089731c548b313394d2` |

The upstream [SWE-bench project license](https://raw.githubusercontent.com/SWE-bench/SWE-bench/02e7a74ffd0b707aab73d203fe87bdc7c76afc8e/LICENSE) is MIT;
[Harbor 0.27.0](https://raw.githubusercontent.com/harbor-framework/harbor/96c86af3bc6d7e92f21fc771d21273be4714c8e9/LICENSE) uses Apache-2.0.
Bootstrap downloads those pinned license texts and the pinned dataset card into
`attribution/`, verifies their hashes and includes them in the input manifest.
Package installs retain wheel license notices. Original Flask license hashes
remain in source provenance.

The data download includes evaluator and reference patches. It stays in the
trusted controller directory. Reference patches are available only to free
qualification controls; evaluator tests belong in the separate verifier image.
The model receives the public issue and allowlisted `src/flask` source tools.
It does not receive Git objects, reference solutions or evaluator inputs.
Source provenance records the Git tree, exported file digest, original license
hashes and the base revision's test requirements. Flask's license remains with
its exported source. Consult the dataset card and original repository licenses
before redistributing task artifacts.

## Create public assets without inference

Run commands from the repository root. Output directories must be unused.
Bootstrap never invokes a model or writes to a Loom index.

```bash
uv venv --python 3.12 .harbor-venv
uv pip install --python .harbor-venv/bin/python --require-hashes \
  -r examples/software_factory/harbor/requirements.txt \
  -r examples/software_factory/evals/flask/bootstrap-requirements.txt
export PYTHONPATH="$PWD:$PWD/sdk/python"
.harbor-venv/bin/python -m examples.software_factory.evals.flask.bootstrap assets \
  --output .benchmark-artifacts/public-flask
.harbor-venv/bin/python -m examples.software_factory.evals.flask.bootstrap build \
  --assets .benchmark-artifacts/public-flask
```

`assets` verifies the dataset and task record hashes, fetches only the three
pinned bases into a bare mirror, writes a public task projection and separate
trusted evaluator files, and creates dependency Dockerfiles with hashed locks.
`build` downloads the pinned Python image and hash-verified packages through the
controller, stages the target-architecture wheels or pinned source distributions,
then builds Docker images with `--network=none` and `pip --no-index`. It records
actual local image IDs, tags, architectures and package artifact hashes in
`images.json`; `dependency-builds.json` records each exact owned tag before its
build begins, including interrupted builds. Neither command performs
inference. Preserve its receipts and do not replace image tags during the study.
Image IDs can differ across architectures or builds; the study freezes the IDs
it actually uses. Qualification determines whether those images are usable.

## Prepare your Loom source index

Set up an isolated Loom container and ClickHouse following
[local development](../../../../docs/development/local-development.md) and
[code index operations](../../../../docs/operations/code-index-operations.md).
Build Loom from your recorded source commit, set `LOOM_BUILD_COMMIT` to that
full commit, and keep that exact source tree available to the controller.
The bootstrap checks its `server/` and `schemas/` bytes against the running
container. A modified source tree is identified by its full file manifest as
well as the build commit; a commit label alone is not evidence of identical code.

Configure six `LOOM_CODE_REPOS_JSON` entries, one Tree-sitter and one SCIP scope
per issue. Each has the same
`repo: "pallets/flask"`, its own namespace, `path_prefixes: "src/flask"`, and
`git_branch` equal to its base commit from the table. The two scopes must have
different namespaces and viewer tokens. Set `git_dir` to the bare
mirror created by `assets`, mounted at the path visible inside your server.
Set `git_remote` to `https://github.com/pallets/flask.git`. For example, one entry
is below; create the other five with their matching tier, namespace and commit.

```json
{
  "org": "factory-example",
  "namespace": "code-flask-4045-treesitter",
  "repo": "pallets/flask",
  "git_branch": "d8c37f43724cd9fb0870f77877b7c4c7e38a19e0",
  "git_dir": "/factory-inputs/flask.git",
  "git_remote": "https://github.com/pallets/flask.git",
  "path_prefixes": "src/flask"
}
```

Run the parser phase for all six entries, using your exact container name.
The three Tree-sitter commands are below; repeat them for the matching `-scip`
namespaces before loading their compiler indexes:

```bash
docker exec your-owned-loom-server loom code backfill \
  --org factory-example --namespace code-flask-4045-treesitter --phase index --no-fetch \
  --commit d8c37f43724cd9fb0870f77877b7c4c7e38a19e0 --context-callees off
docker exec your-owned-loom-server loom code backfill \
  --org factory-example --namespace code-flask-4575-treesitter --phase index --no-fetch \
  --commit bd56d19b167822a9a23e2e9e2a07ccccc36baa8d --context-callees off
docker exec your-owned-loom-server loom code backfill \
  --org factory-example --namespace code-flask-4935-treesitter --phase index --no-fetch \
  --commit fa1ee7066807c21256e90089731c548b313394d2 --context-callees off
```

Do not run compiler ingestion in the Tree-sitter namespaces. Python SCIP setup
is described in the next section. Do not enable embedding, model-generated
descriptions or PR-history ingestion in any scope. Disable code context
descriptions (`LOOM_CODE_CONTEXT_DESCRIPTION_ENABLED=false`), embedding recovery
and automatic compiler/source workers. After explicit setup completes, stop all
source writers before freezing. Do not refresh, ingest or change the indexed
scopes during controls or measured trials. This is an operational freeze with
before/after content audits, not a database-level write denial.

Create one **viewer** token per namespace with your deployment's supported
[token command](../../../../docs/reference/cli.md#tokens). Keep each token in a private
file accessible only to the controller. For example:

```bash
mkdir -m 700 .benchmark-secrets
docker exec your-owned-loom-server loom mint-token \
  --subject factory-reader-4045 --org factory-example \
  --namespaces code-flask-4045-treesitter --roles viewer --ttl-days 7 \
  > .benchmark-secrets/flask-4045-treesitter.token
chmod 600 .benchmark-secrets/flask-4045-treesitter.token
```

Repeat for the other five namespaces. The server must authorize those viewers before the
benchmark. Bootstrap checks each token's actual repository, namespace, base
watermark, build identity and source coverage. The two scopes must contain the
same exact `Task.files` paths and bytes. Compiler declaration and reference
paths must stay inside that allowlist. The SCIP scope needs a matching Python
compiler watermark plus nonzero Python compiler definitions and references;
Tree-sitter scopes must contain no compiler rows. Bootstrap refuses a fallback
that lacks this proof. It never mints tokens itself.

## Build the three source-only Python SCIP indexes

The compiler preparation uses the same exact-base implementation files as the
agent. `assets` writes `compiler/<task>/source`, containing only `src/flask` and
an explicit Pyright configuration, plus an empty dependency-type environment
and `allowed-paths.txt`. It copies no tests, docs, reference patch or Git objects
into that source tree. The indexer uses its bundled standard-library type data;
this protocol's third-party type environment is the pinned empty list.

Use Node **22.23.4** and `@sourcegraph/scip-python` **0.6.6**. The repository's
[toolchain lock](../../../../scripts/code-toolchains/package-lock.json) pins npm
artifacts and integrity hashes; the [Dockerfile](../../../../Dockerfile) pins the
Node tarball and architecture hashes. The converter's Python dependencies come
from `uv.lock` and the `scip` extra. Keep this setup separate from `.harbor-venv`:

```bash
uv sync --locked --python 3.12 --extra code --extra scip
sh scripts/install-code-toolchains.sh
test "$(node --version)" = "v22.23.4"
export FACTORY_ROOT="$PWD"
export FACTORY_COMPILER_ROOT="$PWD/.benchmark-artifacts/public-flask/compiler/pallets__flask-4045"
.venv/bin/python - <<'PYTHON'
import json
import os
import subprocess
from pathlib import Path
from examples.software_factory.evals.flask.compiler import index_command

root = Path(os.environ["FACTORY_COMPILER_ROOT"])
source = json.loads((root / "source.json").read_text())
subprocess.run(index_command(
    root, Path(os.environ["FACTORY_ROOT"]) / "var/code-toolchains/bin/scip-python",
    base_commit=source["base_commit"],
), cwd=root / "source", check=True)
PYTHON
.venv/bin/python scripts/scip_harness/scip_to_delta.py \
  --index "$FACTORY_COMPILER_ROOT/index.scip" \
  --commit d8c37f43724cd9fb0870f77877b7c4c7e38a19e0 \
  --source-root "$FACTORY_COMPILER_ROOT/source" \
  --changed-files "$FACTORY_COMPILER_ROOT/allowed-paths.txt" \
  --producer scip-python --full --dry-run "$FACTORY_COMPILER_ROOT/delta.json"
```

The command explicitly sets `--project-version` to the frozen public base SHA.
The compiler tree has no Git metadata, so deriving a version from Git is invalid.
The shared profile helper constructs the same command in local or bounded Docker
execution; preserve its file hash with the compiler receipts.

Before upload, inspect `source.json`, the indexer output and the delta. Create a
private writer token for `code-flask-4045-scip`; enable the supported SCIP apply
worker for this setup step. The following command validates the file allowlist
and uses the converter's existing upload/status workflow. Fill in your endpoint
and writer-token path; no token value belongs on the command line.

```bash
.venv/bin/python - <<'PY'
import json
import os
from pathlib import Path
from scripts.scip_harness.scip_to_delta import upload

root = Path(os.environ["FACTORY_COMPILER_ROOT"])
source = json.loads((root / "source.json").read_text())
allowed = set((root / "allowed-paths.txt").read_text().splitlines())
delta = json.loads((root / "delta.json").read_text())
assert delta["commit"] == source["base_commit"]
assert delta["files"] and all(row["path"] in allowed for row in delta["files"])
for row in delta["files"]:
    for entry in row.get("symbols", []) + row.get("edges", []):
        assert not entry.get("path") or entry["path"] in allowed
with (root / "delta.json").open() as payload:
    raise SystemExit(upload(
        payload, source["base_commit"], "", "http://127.0.0.1:7777",
        Path(".benchmark-secrets/flask-4045-scip-writer.token").read_text().strip(),
        "scip-python", org="factory-example", namespace="code-flask-4045-scip",
    ))
PY
```

Repeat indexing and upload for 4575 and 4935 using their source directories,
commits and namespaces. Preserve indexer/converter versions, stdout, raw index
and normalized delta with the private study receipts. Source preparation and
SCIP ingestion are operator setup steps; bootstrap protocol creation itself
performs only reads. Stop the apply worker and every source writer afterward.

During the freeze, bootstrap reads live indexed file hashes and requires the
complete path set and content to match the task's exact-base `src/flask` files.
It also checks every live chunk, symbol and reference path and commit against
that allowlist. Compiler watermark text alone cannot qualify a SCIP scope:
there must be nonzero real Python compiler definitions and references at the
base, plus a matching Python compiler-tier status. Tree-sitter scopes require
zero compiler rows. These proofs are recorded separately from the content
fingerprint, which Harbor audits before and after every trial.

## Freeze the protocol and qualify Harbor

Copy [setup.example.json](setup.example.json) to a local
configuration file. Fill in your paths, Loom URL, exact Docker container name,
full server commit and source directory. Paths resolve from the command's
working directory. Set the two operational attestations to `true` only after
stopping source writers and disabling backend generation.

Create `credentials_file` yourself with `ANTHROPIC_API_KEY=...` for an account
that can use **`claude-haiku-5-5`**. The frozen provider is the direct Anthropic
Messages endpoint, `https://api.anthropic.com/v1/messages`, API version
`2023-06-01`; provider gateways and replacement models are separate protocols.
Bootstrap checks that the credentials file exists without reading its contents.
Credentials and token values are excluded from exported configuration and must
not be committed or shared with artifacts.

```bash
cp examples/software_factory/evals/flask/setup.example.json factory-config.json
# Edit factory-config.json and create your private credential/token files.
.harbor-venv/bin/python -m examples.software_factory.evals.flask.bootstrap protocol \
  --config factory-config.json --output .benchmark-artifacts/flask-freeze
.harbor-venv/bin/python -m examples.software_factory.harbor.study prepare \
  --protocol .benchmark-artifacts/flask-freeze/bootstrap-protocol.json \
  --output .benchmark-artifacts/flask-study
.harbor-venv/bin/python -m examples.software_factory.harbor.study base \
  --protocol .benchmark-artifacts/flask-study/protocol.json
.harbor-venv/bin/python -m examples.software_factory.harbor.study reference \
  --protocol .benchmark-artifacts/flask-study/protocol.json
```

Protocol creation performs read-only status requests and Docker index snapshots.
It records the source and input manifests, index certificate and content digest.
Preparation builds agent and separate verifier images, copies the sandbox policy
and hashes runtime inputs. Both free native Harbor controls must qualify against
this exact protocol before `live` can run. The base must fail the independent
regression checks and the reference must pass. Full-suite diagnostics remain
separate from the independent issue-success reward. Missing dependencies,
collection errors or an invalid sandbox are qualification failures to diagnose,
not successful controls. No provider request occurs in either control.

## Explicit paid execution and reports

The next command spends your provider credits. Review `protocol.json`, the
33-slot schedule and the free control receipts before executing it. The protocol
has no dollar ceiling or factory turn/tool deadline: output is bounded by the
provider's 128,000-token response limit, and exact-definition retrieval has a
12,000-token maximum. These policies are identical across all three arms.
The default judge configuration also schedules 12 separate provider requests
after all attempts complete: nine primary triplets and three A/A pairs. Each fresh
conversation assesses whether each anonymized candidate fixes the issue, using the public issue,
base and changed files. It receives no arm identity, retrieval history, costs,
test verdicts or reference patch. The judge uses `claude-haiku-5-5`, seed 741
for presentation order, and a 4,096-token output maximum. It does not rank a
“better solution.” Its subjective verdict
is separate from independent hidden-test success; its cost and time are study
overhead, excluded from agent-generation cost and attempt time. Missing or failed
judgments remain unknown. Review the `judge` configuration before the paid run.

```bash
.harbor-venv/bin/python -m examples.software_factory.harbor.study live \
  --protocol .benchmark-artifacts/flask-study/protocol.json
.harbor-venv/bin/python -m examples.software_factory.harbor.study report \
  --protocol .benchmark-artifacts/flask-study/protocol.json
.harbor-venv/bin/harbor view .benchmark-artifacts/flask-study/jobs
```

The frozen seed is **741**. Three issues times three repeats times three arms make
27 primary attempts; six baseline A/A attempts measure variability. Execution
is serial with retries disabled. Each Loom arm exposes the same five read-only
Loom tools against its certified tier; Claude chooses whether to use them. All
arms have the same ordinary
source tools and authored-regression capability. There is no forced lookup arm.

Reports and bar charts are generated before the first paid trial and after each
completed trial. Open the chart link printed in the report or follow
[Harbor reporting](../../harbor/README.md). Set `publish_charts: false` to keep the
standalone charts without installing the viewer route. Per-attempt, per-scenario
and aggregate JSON/CSV retain failed and incomplete attempts. Native artifacts,
provider request IDs and token usage, retrieval dispatches and index audits
remain available for inspection. Treat controller artifacts as private: they
include task evaluator inputs even though the dataset itself is public.

Issue success comes only from the final independent hidden verifier. Generation
cost uses native uncached standard usage: $0.10/$0.50 per million input/output
tokens through 100,000 input tokens, $0.50/$2.50 above that threshold. These are
frozen comparison rates, not a promise of current invoice prices. Cache, tier or
unknown usage changes are not priced as zero. Loom/backend and infrastructure
costs are separate; absent evidence remains unknown. End-to-end time includes
setup, agent execution, transfer, independent verification, teardown and both
index audits. Agent time is also retained separately.

Compare each Loom tier separately against paired baseline attempts. The target
is 9/9 issue successes with aggregate cost and end-to-end ratios at most 0.80. Report paired uncertainty and A/A noise alongside those
point estimates. A partial cohort, missing costs or an unverified treatment
cannot establish the target. Do not merge on a target that has not been measured.

After inspection, stop only your own deployment and remove only images and
volumes whose exact names/IDs you recorded. Keep failed run receipts and output
folders. A study refuses replaying an existing job; investigate interruptions
before creating another study. This guide does not automatically delete data.

## Export public results without private trial assets

The bootstrap embeds a `public_reproduction` section assembled from the
published suite lock. It contains public repository and task identities,
dataset revision and hashes, model/API version, dependency pins, indexer pins
and the frozen price rule. It includes no credentials, token-file paths,
private endpoints or manual source hints.

Create a public bundle after inspecting the private native results:

```bash
.harbor-venv/bin/python -m examples.software_factory.harbor.public_export \
  --protocol .benchmark-artifacts/flask-study/protocol.json \
  --output .benchmark-artifacts/flask-public --require-complete
```

The command performs no model requests or index writes. It reads the original
native Harbor trial rewards and scalar measurements, preserving all 33 slots,
failed attempts and six A/A controls. The primary tables keep baseline,
Tree-sitter and SCIP separate. A modified private summary cannot override the
native reward. The bundle contains:

- `protocol.json`: the allowlisted public method, schedule, source digests,
  dependency and indexer pins, and source-only index proof counters.
- `attempts.json` and `attempts.csv`: every scheduled slot and measured scalar.
- `summary.json` and `scenarios.json`: primary-arm and A/A aggregates, with
  explicit observed and pending counts.
- `charts/reports/charts.html`: standalone comparison bars, with no viewer
  installation changes. Its JSON data is alongside it.
- `provenance.json` and `manifest.json`: normalized evidence-file labels and
  SHA256 checksums, including checksums of every exported payload file.

Unknown cost, tokens, full-suite or judge evidence remains null. Full-suite
regressions require the original base-control suite. Blind-judge opinions
require the same candidate state as the verifier. Total provider cost combines
the generation estimate with correlated Loom ledger cost; a missing dispatch
join leaves the total unknown. Recorded ledger values are separate from account
invoice reconciliation, and infrastructure cost remains unknown.

`--require-complete` refuses a final bundle until all 33 trials and the native
job have finished. Omit that flag to publish a clearly pending bundle with
missing slots. A completed cohort can still contain unknown measurements or
failed attempts; the exporter makes no performance or merge claim.

The exporter uses explicit field allowlists. It excludes raw configurations,
headers, environment files, credentials, transport bodies, candidate patches,
evaluator test names, private repository text and personal paths. Original
native files remain unchanged, and exported files are copies without hardlinks.
Refreshing an exporter-owned bundle is allowed; unrelated or locally modified
output directories are refused. Keep the private study separately for audits.
An explicit `--public-config` JSON can supply the same public metadata schema
when operating outside the profile bootstrap.

Publish the reviewed public bundle together with the source revision and this
reproduction guide. Other users run the pinned workflow with their own keys;
identical model answers, timings and invoices are not guaranteed.
