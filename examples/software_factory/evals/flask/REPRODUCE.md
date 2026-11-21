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
Two task records come from that dataset. Task 4230 is pinned separately in
[the upstream record](data/upstream-4230.json), with its original issue, source
patch and regression test. The controller selects complete test functions,
including every published parameterization. This also avoids truncated selector
strings in the original CLI record without dropping cases.

| Public task           | Exact Flask base                           |
| --------------------- | ------------------------------------------ |
| `pallets__flask-4074` | `a541c2ac8b05c2b23e11bd8540088fce1abc2373` |
| `pallets__flask-4230` | `4346498c85848c53843b810537b83a8f6124c9d3` |
| `pallets__flask-4642` | `97298e06fe19298c3ff9d2e0ed9ba70bb3fda2c8` |

[Historical cases](memory/historical_cases.json) pair those exact issues with
[the historical corpus](memory/historical_memories.json). Every note names its
public sources; source hashes and chronology are checked before ingestion.
Only history-seeded arms receive the 30 seeded facts. Neither the issue nor the
source or test contract changes between arms. The study contains no authored
replacement issues. Native links are checked at the task's actual indexed base.
All four Loom arms expose identical memory schemas. Cold controls receive private
empty namespaces, proven by native listings; seeded arms replay history from
2.0.0rc2, observe changed links at the task base, then revise one source-backed
fact and verify that its anchor becomes fresh again.

## Execution and artifacts

Three repeats of five arms plus six separate A/A controls produce 51 attempts.
Execution is serial with retries disabled. Twelve blind fixed-only judgments are
separate from native hidden-test rewards. The model chooses all retrieval calls.
Code reads use the serving API defaults for result counts, token limits, search
legs, ranking and body delivery. Every returned body is checked against the local
source span. Code-linked lessons are enabled only for the attempt's bound memory
namespace, including literal grep reads.
Solver and Loom transports share a 300-second request allowance, with retries
disabled. This allowance is separate from measured agent and end-to-end time.

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

These three harder issues were selected before their solver outcomes were
measured. Earlier development outcomes informed harness changes. Five arms run
under one frozen implementation; older scores are not current controls.
Repeated attempts are not independent tasks, and three issues cannot establish
a general result across repositories.

The memory contrast adds historical information as well as retrieval. It tests
that combined contribution; it does not establish the benefit of linking alone.
Exact model outputs, cost and timing can vary. Report every arm, failures, paired
uncertainty, A/A noise and unknowns. Provider cost includes agent and Loom calls;
setup, judging and unpriced infrastructure remain separate. No reduction or
quality gain is established before measurements exist.
