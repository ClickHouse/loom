# Native Flask CLI option-order replay

All five arms pass the hidden tests and the blind judge. Three Loom arms
meet the observed 20% cost and agent-time targets. No arm reaches 20% lower
end-to-end time. Quality ties in this five-attempt cohort.

This replay uses the same frozen Flask issue, reference, historical memory,
hidden and visible tests, native agent and model as the
[first CLI pilot](../native-cli-order-pilot/README.md). The startup change
stabilizes routing text for implicit scopes. The reviewed Python chunk change
associates decorator hits with their owning function. The base and reference
controls earn the expected rewards of zero and one.

[Measured attempts and verified input hashes](results.json)

| Arm                  | Hidden solved | Blind fixed | Regressions | Provider USD | Agent seconds | End-to-end seconds |  Tokens | Loom calls / total |
| -------------------- | ------------: | ----------: | ----------: | -----------: | ------------: | -----------------: | ------: | -----------------: |
| No Loom              |           1/1 |         1/1 |           0 |    0.1693688 |        31.675 |             47.165 | 139,085 |              0 / 8 |
| Tree-sitter          |           1/1 |         1/1 |           0 |    0.0974976 |        25.051 |             44.216 | 147,237 |              2 / 5 |
| Tree-sitter + memory |           1/1 |         1/1 |           0 |    0.0950546 |        20.040 |             38.688 | 123,093 |              2 / 4 |
| SCIP                 |           1/1 |         1/1 |           0 |    0.1835900 |        23.984 |             42.870 | 148,600 |              1 / 5 |
| SCIP + memory        |           1/1 |         1/1 |           0 |    0.0820518 |        21.683 |             40.553 | 150,252 |              2 / 5 |

Solver cost totals USD 0.6275628. The blind judge costs USD 0.346012.
No backend provider cost is attributed to Loom requests. Local infrastructure
and embeddings are unpriced. No coding agent writes memory.

## Prompt cache

| Arm                  | First cache-read tokens | First cache-write tokens | Observed cost change | Agent-time change | End-to-end change | Equal cold first-input cost change | Equal warm first-input cost change |
| -------------------- | ----------------------: | -----------------------: | -------------------: | ----------------: | ----------------: | ---------------------------------: | ---------------------------------: |
| No Loom              |                       0 |                   17,624 |                +0.0% |             +0.0% |             +0.0% |                              +0.0% |                              +0.0% |
| Tree-sitter          |                  17,833 |                    4,462 |               −42.4% |            −20.9% |             −6.3% |                              +8.1% |                             −10.3% |
| Tree-sitter + memory |                  17,833 |                    4,453 |               −43.9% |            −36.7% |            −18.0% |                              +6.7% |                             −13.1% |
| SCIP                 |                       0 |                   22,297 |                +8.4% |            −24.3% |             −9.1% |                              +8.4% |                              −9.7% |
| SCIP + memory        |                  22,286 |                        0 |               −51.6% |            −31.5% |            −14.0% |                             +11.6% |                              −3.2% |

The sensitivity changes the price of the first prompt only. Later requests
and usage use the recorded values. Every Loom arm costs more than baseline
in the equal-cold estimate. The largest equal-warm cost reduction is 13.1%.
The observed cost comparison reflects provider cache reuse.

Both CLI cohorts run the arms in the same order. SCIP is first in both and
records zero first-prompt cache reads. These repeats share the same order
effect. The full schedule rotates treatment positions across task blocks.

## End-to-end timing

Tree-sitter with memory spends 11.63 fewer seconds in the agent than No Loom.
Environment setup, agent setup and verification together differ by 0.07 seconds.
Time outside those three phases and agent execution is 7.60 seconds with
Tree-sitter and memory and 4.51 seconds with No Loom. That residual accounts
for most of the difference between the agent-time and end-to-end savings.

## Tool evidence

The two memory connections have identical startup instruction fingerprints.
All four Loom connections advertise the same tool catalog. In the Tree-sitter
memory attempt, grep assigns the previously unscoped option declaration to
the owning function. The same query returns one linked note, down from three
in the first pilot. The callback-order note still repeats the note read by
the earlier symbol call.

## Reproduction

Use the [frozen replay revision](https://github.com/ClickHouse/loom/tree/d3f6faa1761f1c04c2ce49b9f1ed6303d9802cd3)
and follow its [CLI pilot instructions](https://github.com/ClickHouse/loom/blob/d3f6faa1761f1c04c2ce49b9f1ed6303d9802cd3/examples/software_factory/evals/flask/CLI-ORDER-PILOT.md).
This cohort has one attempt per arm and no A/A controls. Its timing and cost
are descriptive, and its quality tie leaves the historical memory target open.
