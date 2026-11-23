# Reproduce the Flask factory comparison

Run the frozen software factory with host Python 3.12, Docker with Compose and an
Anthropic API credential. The launcher builds its pinned controller environment
and automates service setup, source indexes, memory ingestion and Harbor jobs.
A separate prepared Loom deployment is not required.

```bash
python3 -m examples.software_factory plan --eval flask --json
ANTHROPIC_API_KEY=... python3 -m examples.software_factory run --eval flask --output ./factory-run
```

Use a fresh output directory. Model changes define separate cohorts; compare arms within
one cohort, and retain results from earlier models. The solver and blind judge use
`claude-opus-5-5`. Loom model-backed operations use its separately recorded default
`claude-sonnet-5-5`. Native coding runs have no dollar cap; capped launches are refused. An interrupted phase refuses automatic replay;
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
Every Loom arm exposes the same native coding tool roster. Only the two memory
arms have private writable memory namespaces; they replay history from
2.0.0rc2, observe changed links at the task base, then revise one source-backed
fact and verify that its anchor becomes fresh again.
Revision changes retire text paths removed from the verified Git source through
Loom's native file-deletion path before backfilling the next revision. Source
receipts record the exact paths and retirement counts; the complete index must
still match the current source.

The indexed text roots are `src/flask`, `tests` and `docs`, for every Loom arm.
They contain only the original Git revision; evaluator patches are absent.
Hidden paths and non-text fixtures remain in the local checkout. Native index
exclusions match the exporter's hidden-path rule. The compiler uses the same public
roots, and native receipts disclose files its parsers decline. Candidate grading
continues to accept implementation changes under `src/flask` only. Expanding index
coverage defines a separate cohort from implementation-only indexes.

## Execution and artifacts

Three repeats of five arms plus six separate A/A controls produce 51 attempts.
Harbor uses its built-in `claude-code` agent, pinned by
[agent.json](../../harbor/agent.json). Every arm receives the original public
issue in an ordinary shallow checkout at its buggy base. Claude chooses its
reads, edits, commands, retrieval, subagents and stopping point. There is no
factory coding loop, injected source context, forced Loom call or final review turn.

Every arm receives identical conditional retrieval guidance in `/workspace/CLAUDE.md`,
including the no-Loom arm. The [shared template](../../harbor/project-guidance.md)
routes supported lookups through Loom when configured and permits local editing,
tests and fallback. The protocol and public reproduction manifest retain its exact
text and SHA-256, and the fairness audit checks every task image's staged copy.
This is an instructed cohort; keep it separate from runs without these instructions.
The file contains no issue-specific hints, solution or hidden-test information.

The model, Claude Code version, checkout image, one CPU and 2 GiB agent memory
are shared across arms for each issue. During coding, the sandbox allows the
provider endpoint and, for Loom arms, their Loom endpoint. Hidden tests and
reference fixes remain in separate evaluator containers with no network,
one CPU and 512 MiB. Agent-visible Git history contains only the task base.

Native MCP configuration selects the code namespace and optional historical
memory namespace. Retrieval uses Loom defaults; no result-count or token-budget
overrides are supplied. Each attempt has separate credentials and each memory
attempt has separate writable state. Base/reference controls must qualify the
same frozen protocol before inference. Harbor retries are disabled; Claude Code
keeps its native provider behavior. Twelve blind fixed judgments run after coding
and do not send feedback to the solver.

Native MCP connections send `Connection: close`. The pinned egress proxy can reuse
an upstream HTTP connection after the server has closed it, causing the next tool
request to reset before reaching Loom. Closing each request avoids that stale
connection across all Loom arms, without retries or execution deadlines. This
does not change Loom's server defaults or the tools available to Claude.

Harbor trajectories supply tool calls, tokens and native reported cost, with a
marked estimate when reported cost is unavailable. Agent time is Harbor's native
agent execution interval. End-to-end time also includes setup, artifact transfer,
hidden verification and index audits. Explicit test commands can be counted from
shell calls; commands that hide tests inside scripts are not classified as test runs.

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
