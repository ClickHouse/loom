# SDK issue resolution

The SDK issue harness measures proposed patches with and without Loom code lookup.
Both arms use the same model, system instructions and local search, read and exact-edit
tools. The treatment adds one binding to the installed Loom Python SDK. Neither agent
can execute tests or shell commands. A separate evaluator grades both patches with the
same selected tests and timeout.

Both local search tools accept `context_lines` from 0 to 20 and return at most 10,000
characters. A literal lookup can therefore supply enough surrounding code for an edit
without requiring a second read.

The conditional instruction profile uses indexed definitions, relationships and concepts
first when lookup is available. Literal searches and working-tree changes use local
tools; an insufficient index permits local fallback. Supplied evidence is reused.
No tool call is required merely to demonstrate adoption. Zero-use and failed runs remain
in the result set. The treatment lists its indexed lookup first, followed by the same
local tools in their original order; the manifest records this instruction and ordering profile.

This protocol exercises the code-index integration. It performs no memory warm-up or
reuse, measured-coverage import or test-gate evaluation. It cannot establish whole-product
efficacy or isolate a plugin problem. The target is correct patches with lower time and
token use; neither availability nor this instruction profile guarantees adoption or a win.

## Prepare inputs

Install the repository's `aws` extra for `boto3`, and install `sdk/python` into the same
environment with `uv pip install --python .venv/bin/python -e sdk/python`. The harness
uses the ordinary `loom.Agent` import and existing cloud IAM credentials. It adds no SDK
provider adapter. Git and ripgrep must be available. Process supervision uses POSIX
process groups.

Use an isolated clean checkout at the exact issue base for each arm. Keep evaluator
files, patches and output directories outside both agent checkouts. A sanitized task
contains only public metadata and the issue text:

```json
{
  "instance_id": "owner__repository-issue",
  "repo": "owner/repository",
  "base_commit": "<full commit SHA>",
  "problem_statement": "<public issue text>"
}
```

Optional public fields are `version`, `environment_setup_commit` and `created_at`.
Unknown fields, including reference and test patches, are rejected. Public task data is
available from [SWE-bench Verified](https://huggingface.co/datasets/princeton-nlp/SWE-bench_Verified).
Selecting this dataset does not make a native evaluator an official-container score.

Prepare a private grader configuration shared by both arms:

```json
{
  "command": [
    "python",
    "<absolute evaluator script>",
    "--patch",
    "{patch}",
    "--report-to",
    "{output}"
  ],
  "selected_tests": ["tests/test_behavior.py::test_case"],
  "timeout_s": 120
}
```

The command runs outside the agent process, without a shell. Supported substitutions
are `{patch}`, `{output}`, `{run}`, `{arm}` and `{instance_id}`. The evaluator applies the
patch in its own clean checkout and keeps held-out data outside agent inputs. Its working
directory is the run's `grading/` directory; use absolute evaluator and dependency paths. Establish
base failures and reference-patch passes before inference. Freeze dataset selection,
dependencies, index tier and per-issue index commits in the experiment plan. The treatment
checks its live index commit before its first model call. Static edges remain lower bounds.

## Run and grade

Model calls spend provider money. Obtain approval for the issue count, repeats, model
and arms before running. Index preparation and backend inference have separate costs.
Use the same flags for both arms except `--arm`, `--checkout` and `--out`:

```bash
PYTHONPATH=$PWD .venv/bin/python -m bench.sdk_issue_resolution run \
  --task task.json --checkout <clean checkout> --expected-base <full commit SHA> \
  --arm baseline --model <Bedrock model ID> --region <region> \
  --org <org> --namespace <code namespace> --base-url "$LOOM_BASE_URL" \
  --token-env LOOM_API_KEY --grader-config grader.json --out <unused run directory>
```

The Loom arm uses `--arm loom`. Token values are read from the named environment
variable and are absent from the manifest. A run directory is created exclusively;
existing runs cannot be overwritten or silently retried. The manifest freezes task,
instructions, tool schemas, dependency versions, runner hashes, limits and grader plan
before inference. The default agent timeout is 110 seconds; give an outer resource
supervisor enough additional time for artifact finalization. No provider or SDK transport
retries or prompt-cache checkpoints are configured.

Run the evaluator after each arm finishes, including empty or partial patches:

```bash
PYTHONPATH=$PWD .venv/bin/python -m bench.sdk_issue_resolution grade --run-dir <run directory>
PYTHONPATH=$PWD .venv/bin/python -m bench.sdk_issue_resolution compare \
  --baseline <baseline run directory> --loom <Loom run directory>
```

The evaluator must write `{output}` as JSON with explicit `valid`, `resolved`,
`selected_test_statuses` and `missing_tests` fields. Statuses are keyed by the frozen
selectors and use `PASSED`, `FAILED` or `ERROR`. If a dataset selector abbreviates parameter
IDs, the evaluator must retain the mapping to actual collected nodes and require all
matching outcomes to agree. Missing results, missing selected tests, skipped outcomes,
timeout and invalid grading are `invalid_setup`, never a pass. Exit zero alone is not
resolution. A valid unresolved report stays unresolved.

## Read artifacts

`MANIFEST.json` is the pre-inference protocol. `events.jsonl` preserves model responses
and all reported usage fields, failed tool results, SDK request IDs, lookup wall time and
returned evidence. `agent.diff` preserves tracked and new-file changes even after a
provider error, permission refusal, turn limit or supervised timeout. `RESULT.json`
separates execution status from patch presence and external resolution. A completed
empty patch is labelled `completed_empty_patch`; `resolved` remains null there.
`stage`, `preflight_status` and `preflight_failed` identify a binding preflight failure.
`agent_inference_started: false` records a failure before the agent inference loop;
true marks a loop attempt, and null means progress is unknown. Missing response usage
does not establish zero billing for an attempted provider call.

SDK HTTP 401, 403 and 409 refusals and local permission errors have `guard_refused`
status, including refusals from the index preflight. Cleanup errors retain an earlier
guard refusal. Tool-time SDK refusals retain their HTTP status, code and request ID.
Patch collection errors stay in `patch_errors` without replacing a refusal. A refusal
originating in cleanup records `provider_cleanup` or `sdk_cleanup` as its stage.
Recorded terminal, cleanup and denied-tool evidence keeps the first refusal primary
even when supervision later times out, is interrupted or reports a nonzero exit.
`supervision` retains that outer status, return code and error separately. A successful
terminal event cannot override failed supervision when no refusal was recorded.
A controller must stop before grading or starting another trial; it
must also check the serialized `error.http_status` when reading results from another
worker version. A failed preflight cannot establish a paired performance result.
The refusal does not authorize switching credentials, scopes or retrying the action.

Setup refusal leaves an empty patch and preserves the checkout. When a lookup has no
result, `sdk_lookup_wall_s` is null and `sdk_lookup_timing_complete` is false.
`sdk_completed_lookup_wall_s` retains the measured completed-call sum;
`sdk_unfinished_lookups` counts calls whose duration is unknown.
`GRADE.json` stores external grading separately. A hard process kill can leave a partial
last event; complete earlier events are retained.

Report every attempted run, including zero adoption, errors and invalid grading. Preserve
cache-read and cache-write fields alongside ordinary input/output counts. Agent model
usage is separate from SDK lookup latency, backend model/embedding usage and cold index
preparation. Do not call agent-only usage total system cost. Compare only matching
protocols; the comparison command rejects mismatched tasks, instructions, models, grader
plans or limits. Repeated runs of one easy issue do not establish general benefit.
Even a complete split from one project is a small sample, and repeated attempts on the
same issue are not independent tasks. Keep negative, zero-use and failed attempts in
their original cohorts. A setup or instruction change starts a separately frozen cohort.
