# Native source-routing cohort

All five arms completed three repeats of the same three historical issues: 45
primary attempts and six baseline A/A controls. Harbor runs stock Claude Code
with Opus 5.5. Every arm receives the same conditional project instructions:
use Loom for unchanged indexed source when configured, and local tools otherwise.
This measures instructed usage, not unprompted adoption.

| Arm                    | Hidden solved | Blind fixed | Mean provider USD | Mean agent seconds | Total tools | Loom calls |
| ---------------------- | ------------: | ----------: | ----------------: | -----------------: | ----------: | ---------: |
| No Loom                |           6/9 |         7/9 |         0.2347622 |             51.667 |          79 |          0 |
| Tree-sitter            |           5/9 |         7/9 |         0.2585068 |             55.442 |          91 |         17 |
| Tree-sitter and memory |           4/9 |         5/9 |         0.2590987 |             55.423 |          97 |         28 |
| SCIP                   |           4/9 |         6/9 |         0.2169546 |             47.866 |          73 |         20 |
| SCIP and memory        |           3/9 |         5/9 |         0.2747968 |             58.692 |         102 |         32 |

No arm meets the combined quality, cost and time target. SCIP's observed means
are 7.6% cheaper and 7.4% faster than baseline, with fewer hidden solves. Neither
memory arm improves hidden or blind-judge quality over its matching index arm.
All primary attempts have zero full-suite regressions and zero agent memory
writes. All 36 Loom attempts invoke Loom; all 97 selected calls succeed. All
18 memory-enabled attempts receive lessons. The corpus has 30 historical facts,
14 linked to code. Three issue clusters and baseline A/A outcomes of 1/3 versus
2/3 limit treatment-effect conclusions.

Costs are provider-price estimates from native usage plus attributable Loom
provider usage, not invoices. They exclude one-time setup, indexing, local compute
and separate judging. Coding including baseline controls costs USD 12.4995124;
judging costs USD 8.5112520. Their total is USD 21.0107644. Retrieval uses a local BGE-small embedder; these requests
incur no paid Loom model charge. Agent time excludes setup and grading. Per-attempt
E2E time, tokens, cache usage, test runs, full-suite regressions and memory writes
are available in [attempts.csv](attempts.csv) and [results.json](results.json).
The judge measures whether a candidate fixes the issue; no better-solution rating
is configured. Judge identities are blinded, but its reasoning can draw on
knowledge of the historical upstream fix. Its rating remains separate from tests.

## Initial cache sensitivity

The read-only sensitivity plan was registered before any solver results. Observed
costs remain unchanged. Replacing only each first request's input charge with a
uniform cold-write or warm-read charge removes SCIP's observed cost advantage:
it becomes 8.5% or 1.7% more expensive than baseline, respectively. All first-call
price reconstructions reconcile. These are counterfactual cost sensitivities,
not invoices, controlled-cache measurements or measured speedups. See
[initial-cache-sensitivity.json](initial-cache-sensitivity.json).

## What the hidden score covers

The frozen selectors faithfully inherit the upstream benchmark's fail-to-pass and
pass-to-pass cases. An independent review read each original issue before its
grader, without reading attempt outcomes or solutions. It found scope limitations:

- The multi-mount blueprint issue asks for relative URL generation to distinguish
  mounts. Selected tests also require exact collision warnings, exception types,
  a naming API and immediate self-registration rejection. They contain no direct
  assertion of multi-mount relative URL generation. The broader suite checks new
  endpoint names.
- The nested-callback issue names four forward hook families. Its new test also
  requires inverse teardown order, which differs from the base revision. This is
  a defensible lifecycle contract, but absent from the issue text. The new test
  does not directly assert two of the named forward hook families.
- The nested CLI context issue's added test matches its reported factory-context
  problem, with limited coverage of other nesting combinations.

Every frozen score, selector and outcome is retained. This audit does not rescore
failures or establish that a candidate is correct. Future paid cohorts must state
acceptance contracts and check their coverage before execution. Historical memory
must not contain later hidden requirements or solutions.

## Reproduce

This cohort executes
[frozen source](https://github.com/ClickHouse/loom/tree/53bcb61d186099b98abca5acd3bc765c82687ea0).
Use that revision's
[reproduction instructions](https://github.com/ClickHouse/loom/blob/53bcb61d186099b98abca5acd3bc765c82687ea0/examples/software_factory/evals/flask/REPRODUCE.md).
The source roots contain implementation code; all arms can access the same public
repository documents and tests locally. Fresh conversations and isolated attempt
scopes prevent agent results from carrying between attempts. Provider prefix
caching remains a separate cost consideration. Model outputs and latency vary.

The sanitized native export preserves input fingerprints and artifact hashes.
Its original `results.json` SHA-256 is
`6fa2ff134d7b4f11af106c6d6de7ef52a3e48c21caa9d4805e8227180ae976e6`.
Repository formatting can change bytes while preserving parsed values; the
manifest verifies the published files. Native trajectories, credentials and raw
provider-ledger rows remain private.
