# Reproduce the Flask factory comparison

Run the frozen software factory with host Python 3.12, Docker with Compose and an
Anthropic API credential. The launcher builds its pinned controller environment
and automates service setup, source indexes, memory ingestion and Harbor jobs.
A separate prepared Loom deployment is not required.

```bash
python3 -m examples.software_factory plan --eval flask --json
ANTHROPIC_API_KEY=... python3 -m examples.software_factory run --eval flask --output ./factory-run
```

Use a fresh output directory. The default model is `claude-haiku-5-5` for the solver,
native model-backed operations and blind judge. Runs have no dollar cap unless
`--budget-usd` is supplied. An interrupted phase refuses automatic replay;
inspect retained receipts before explicit resume or cleanup:

```bash
ANTHROPIC_API_KEY=... python3 -m examples.software_factory resume --output ./factory-run
python3 -m examples.software_factory cleanup --output ./factory-run
```

## Frozen inputs

[suite.lock.json](suite.lock.json) pins the public SWE-bench dataset revision,
record hashes, source commits, test and dependency image inputs, package hashes,
compiler toolchain and model. [Harbor requirements](../../harbor/requirements.txt)
pin Harbor 0.27.0 separately from server dependencies. Original source license
hashes and downloaded dataset attributions remain in the asset receipt.

| Public task           | Exact Flask base                           |
| --------------------- | ------------------------------------------ |
| `pallets__flask-4045` | `d8c37f43724cd9fb0870f77877b7c4c7e38a19e0` |
| `pallets__flask-4575` | `bd56d19b167822a9a23e2e9e2a07ccccc36baa8d` |
| `pallets__flask-4935` | `fa1ee7066807c21256e90089731c548b313394d2` |

[Historical cases](memory/historical_cases.json) pair those exact issues with
[the historical corpus](memory/historical_memories.json). Every note names its
public sources; source hashes and chronology are checked before ingestion.
Only memory-enabled arms receive the 24 seeded facts. Neither the issue nor the
source or test contract changes between arms. The study contains no authored
replacement issues. Native links are checked at the task's actual indexed base.

## Execution and artifacts

Three repeats of five arms plus six separate A/A controls produce 51 attempts.
Execution is serial with retries disabled. Twelve blind fixed-only judgments are
separate from native hidden-test rewards. The model chooses all retrieval calls.
Code reads use the serving API defaults for result counts, token limits, search
legs, ranking and body delivery. Every returned body is checked against the local
source span. Code-linked lessons are enabled only for the attempt's bound memory
namespace, including literal grep reads.

Each attempt has a source-only checkout and an isolated test environment. Agent
and verifier containers have no network, one CPU, 512 MiB and 64 processes. The
agent cannot inspect Git objects, reference patches or the hidden verifier.
Base/reference controls must qualify the same frozen protocol before inference.

The trusted controller retains the dataset's evaluator and reference patches.
Runtime outputs and task tokens belong outside version control. The resource
journal identifies every owned container, image, volume and network. Cleanup
uses exact recorded identities and retains reports; it never prunes Docker.

The study directory holds immutable inputs, qualification receipts, raw Harbor
trials, provider usage, native request IDs, code/memory receipts, trajectories and
[incremental reports](../../harbor/README.md). A standalone chart report works
without a viewer. Reporting can publish derived native jobs and chart assets to a
configured viewer; the original measured jobs remain untouched.

## Interpretation

This is a development regression study: earlier task outcomes informed analysis.
The five arms are rerun under one frozen implementation; an older score is not a
current control. Three issues are a subset of the eleven Flask issues in the
pinned dataset, and repeated attempts are not independent tasks.

The memory contrast adds historical information as well as retrieval. It tests
that combined contribution; it does not establish the benefit of linking alone.
Exact model outputs, cost and timing can vary. Report every arm, failures, paired
uncertainty, A/A noise and unknowns. Provider cost includes agent and Loom calls;
setup, judging and unpriced infrastructure remain separate. No reduction or
quality gain is established before measurements exist.
