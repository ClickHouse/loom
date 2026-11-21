# Python E2E evaluation: Flask

The factory runs the same coding-agent loop with ordinary source tools alone
or with optional read-only Loom tools backed by Tree-sitter or SCIP. Harbor owns historical Flask trial
scheduling, Docker environment lifecycle, independent verification and its local
viewer. An offline replay exercises the loop without model inference.

## Historical Flask evaluation with Harbor

Follow [REPRODUCE.md](REPRODUCE.md) to create the initial protocol from pinned
public SWE-bench tasks, build dependency images, configure your own Loom endpoint
and viewer tokens, and run free Harbor controls before paid inference. No private
predecessor protocol or image supplied by a maintainer is required.

The study pins Harbor 0.27.0, three public issues at exact Flask revisions,
three paired repeats across baseline, Loom Tree-sitter and Loom SCIP, plus six
baseline A/A controls: 33 scheduled attempts.
The provider is direct Anthropic Messages using `claude-haiku-5-5`.
Execution is serial with retries disabled. Reports, CSV/JSON exports and bar
charts are generated before the first paid trial and after each completed trial;
see [Harbor reporting](../../harbor/README.md).

Preparation freezes task records, source and dependency manifests, local image
IDs, evaluator inputs, runtime files, the schedule and the source-index snapshot.
Both native base/reference controls must qualify against that exact protocol
before the explicit `live` command can perform inference. A study refuses to
replay an existing job. Preserve failures and investigate interruptions before
creating another study.

Each trial uses a fresh Docker agent environment and a separate verifier.
The model receives the public issue and allowlisted `src/flask` source tools.
Its tools cannot read reference solutions, evaluator inputs or Git objects.
The final independent hidden verifier decides issue success. Full-suite results
are separate diagnostics. Authored tests provide development feedback and never
add another quality score or success gate.

The frozen [sandbox policy](../../harbor/sandbox-compose.yaml) sets no network, 64 processes,
one CPU and 512 MiB through the policy and task configuration. Containers drop
all capabilities except those needed to drop the test child's identity and reap
its process group, with privilege escalation disabled. The separate verifier
owns the same policy in its task definition.

End-to-end time includes setup, the factory loop, transfer, independent
verification, teardown and both index audits. Agent time and native Harbor wall
time are also retained. Generation cost uses native provider usage and the
frozen pricing rule. Backend and infrastructure costs remain separate; missing
billing evidence is unknown. Failed and incomplete attempts remain in exports.
A partial cohort has no final comparison.

Each Loom tier is compared separately with baseline. The target is 9/9 issue
successes with at least 20% lower aggregate
generation cost and end-to-end time against paired baseline attempts. Report
paired uncertainty and A/A noise alongside the point estimates. These targets
are acceptance criteria, not claims that an unmeasured run meets them.

## Modes and source tools

| Mode | Factory behavior | Model behavior |
| --- | --- | --- |
| `baseline` | Ordinary source tools; no Loom backend or schema | Reads, searches, edits and tests source |
| `natural` | The same tools plus five read-only Loom tools, using the slot’s certified Tree-sitter or SCIP index | Chooses whether to use Loom; zero calls is valid |

There is no enforced retrieval mode or host lookup quota. All arms receive the
same public task instructions and authored-regression capability. Cold attempts
have no cross-issue memory. Retrieval writes no memory rows.

`factory/workspace.py` limits source reads and edits to the task manifest. Each attempt
has a source-only checkout. Paging preserves complete source authority while
bounding individual read responses. `factory/context.py` and `factory/retrieval_tools.py` verify
returned definitions and literal lines against the exact local source, preserve
partial coverage, and reject stale bodies or navigation through edited files.
Every real Loom call checks the repository, namespace, full commit and serving
build before and after retrieval. Permission uncertainty fails closed.

A grep/search prefix that cannot match any task source returns a local
`outside_factory_source_scope` refusal. It reports `server_coverage: not_queried`
and `request_dispatched: false`; this does not claim the server has no coverage.
The selected call still counts, while logical and physical Loom requests remain
zero. Prefixes that could match source still require all scope and authorization
checks. Tool selections, logical operations, physical retrieval requests and
scope-check requests remain distinct counters.

The live grader exposes `regression_test(source)` equally in all arms. It
accepts at most 16,000 UTF-8 bytes of self-contained pytest derived from public
requirements. The test runs outside the candidate checkout as a nonroot child,
with a 30-second process-group deadline and at most 4,000 characters of failure
output. Collection errors, skips, empty files and timeouts never report a pass.
Authored tests cannot change candidate source or fixed evaluator tests. Their
results do not change fixed-test verification state or the independent grade.
They count as ordinary tool calls; the offline finite-expression grader offers
no arbitrary Python execution.

See [the evaluation architecture](../../../../docs/architecture/software-factory-evaluation.md)
for the external dependency and verification boundaries, and
[the pilot protocol](PILOT.md) for the measurement policy.

## Run the offline fixture

From the repository root with Python 3.12:

```bash
python -m examples.software_factory.tests.smoke run \
  --task name-validation --mode natural --output /tmp/factory-one
python -m examples.software_factory.tests.smoke smoke --output /tmp/factory-smoke
python -m examples.software_factory.tests.smoke compare \
  /tmp/factory-smoke/name-validation-baseline/result.json \
  /tmp/factory-smoke/name-validation-natural/result.json \
  --output /tmp/factory-comparison.json
python -m unittest examples.software_factory.tests.factory.test_factory -v
```

Choose unused output directories. The three authored tasks cover name
validation, path composition and HTTP method normalization. `tests/fixtures/python/suite.json` pins
the tasks, scripted actions and independent grader inputs. The replay reads,
edits and tests source, but its scripted patches measure no model reasoning,
quality gain, adoption or live savings. It makes no network requests and needs
no credentials, database or third-party packages. Adding credentials does not
turn the offline CLI into a paid run.

The offline evaluator parses a finite scalar-expression subset; it performs no
Python `exec`, imports, filesystem access or subprocess calls. Candidate Python
for historical tasks runs only inside the qualified Docker environment. Never
add a host subprocess executor for candidate code.

`historical-candidates.json` retains imported control evidence for inspection;
its receipts are not current qualifications. The public bootstrap lock and the
actual native Harbor controls define the executable historical route.

## Validation and artifacts

Install the pinned Harbor dependencies before the complete unit suite:

```bash
python -m unittest discover -s examples/software_factory/tests -t . -v
```

The process-supervision tests require Linux `os.waitid`; run the complete suite
on Linux. Dependency-free fixture tests can run separately with the standard
library. Unit tests and replays do not call providers or ingest an index.

Runtime outputs belong outside version control. Preserve the protocol,
qualification receipts, native Harbor jobs, provider usage, retrieval events,
index audits and automatic reports together. Keep credentials and task-scoped
tokens private. Remove only resources whose exact ownership you recorded;
cleanup never grants permission to delete another run's data.

The repository-first suite lock declares `languages: ["python"]` and
`source_roots: ["src/flask"]`. These describe this Flask profile. Future
repositories can declare several languages in one suite and retain one end-to-end
result per issue. Python/pytest and SCIP Python proof are adapter-specific here.
See [result artifact layout](results/README.md).

Test requests and executed runs are separate counters. `agent_test_requests`
includes rejected tool choices. `agent_test_runs` counts visible verification
receipts initiated by the agent and authored-test execution receipts, including
failed and timed-out runs. Syntax or argument refusals count only as requests.
An execution failure without a grader receipt leaves the run count unknown.
Automatic final verification is recorded separately.

The initial model prompt contains only the public issue plus repository, base
revision and source paths. An operator-selected symbol is used for offline
fixtures and index qualification, and is not supplied to the model.

Code-read requests omit `token_budget`, and source search omits `top_k`. Both
Loom tiers use the serving API defaults pinned by the frozen server source.
Search retains its result cards, `has_more` and `next_cursor`; Claude can request
following pages. Lexical-only search and disabled model-backed search legs are
unchanged. Complete returned bodies still require exact local span grounding.
