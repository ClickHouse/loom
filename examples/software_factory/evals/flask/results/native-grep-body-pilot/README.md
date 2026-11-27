# Native grep body and scoped lesson pilot

Harbor runs one attempt per arm on the same historical Flask callback-order issue
using stock Claude Code 2.1.326 and `claude-opus-5-5`. The serving revision is
[396c43737ea727aa1377a25193be739ac4a2a4bd](https://github.com/ClickHouse/loom/tree/396c43737ea727aa1377a25193be739ac4a2a4bd).
The issue, verification, history and arm order match the preceding
[five-arm pilot](../native-five-arm-pilot/README.md).

| Arm                  | Hidden solved | Blind fixed | Full-suite failures | Regressions | Provider USD | Agent seconds | End-to-end seconds |
| -------------------- | ------------: | ----------- | ------------------: | ----------: | -----------: | ------------: | -----------------: |
| No Loom              |           0/1 | Yes         |                   1 |           0 |    0.1972066 |     49.925186 |             65.128 |
| Tree-sitter          |           0/1 | Yes         |                   1 |           0 |    0.1981712 |     48.034132 |             66.151 |
| Tree-sitter + memory |           0/1 | Yes         |                   1 |           0 |    0.2949854 |     50.775428 |             68.653 |
| SCIP                 |           1/1 | No          |                   0 |           0 |    0.1814546 |     39.031466 |             56.989 |
| SCIP + memory        |           0/1 | Yes         |                   1 |           0 |    0.2170348 |     54.459531 |             72.788 |

| Arm                  | Model responses | Total tokens | Tools | Loom calls | Agent test commands | Agent memory writes |
| -------------------- | --------------: | -----------: | ----: | ---------: | ------------------: | ------------------: |
| No Loom              |              10 |      221,409 |     9 |          0 |                   3 |                   0 |
| Tree-sitter          |              10 |      283,715 |     9 |          4 |                   3 |                   0 |
| Tree-sitter + memory |              10 |      280,103 |     9 |          1 |                   4 |                   0 |
| SCIP                 |               9 |      253,642 |     8 |          3 |                   2 |                   0 |
| SCIP + memory        |              12 |      346,700 |    11 |          2 |                   3 |                   0 |

Solver provider cost totals USD 1.0888526. The separate blind judgment costs
USD 0.800308, bringing measured provider cost to USD 1.8891606. Attributed Loom
provider charges are zero. Local infrastructure and embedding have no price
assigned. [Exported measurements](results.json)

## What the traces show

- Tree-sitter and SCIP asked for enclosing bodies. Each received six definitions
  in one grep call and skipped a separate local read of those definitions.
  Tree-sitter read an indexed test function locally after a line-only lookup.
  SCIP used its remaining local calls for editing, verification and the changelog.
- Tree-sitter with memory retrieved one scoped lesson of 243 excerpt characters.
  SCIP with memory retried a failed regex using the broader literal
  `blueprints`. The first page included an unscoped match, so Loom kept six
  lessons totaling 1,364 excerpt characters. Both agents read callback bodies
  through local commands.
- Three of the four Loom arms sent an invalid regex before a successful lookup.
  A list of literal alternatives expresses those fragments in one call.
  [Literal alternatives](https://github.com/ClickHouse/loom/pull/1249)
- Tree-sitter with memory cost USD 0.0977788 more than baseline. Cache writes
  account for USD 0.087645 of that difference. That is an accounting split
  for this attempt. Cache state and subsequent work both vary between attempts.
- The four hidden failures share the same teardown-order assertion. Only the SCIP
  patch changes teardown. The blind judge treats it as beyond the scope of the
  public issue and accepts the other four patches. The two measures capture
  different requirements here. The original hidden grade remains the primary
  executable outcome.

SCIP cuts agent time by 21.8% and end-to-end time by 12.5%. Its observed cost
reduction is 8.0%. No arm reaches both 20% targets. Neither history arm improves
hidden-test success in this sample. The full matrix remains held. The traces
identify failed requests and delivered context. Repeatable efficiency
and a quality benefit from memory still need paired confirmation across issues.

## Reproduction

Use the frozen revision and the same
[factory instructions](../../REPRODUCE.md) with `--pilot-task pallets__flask-4230`.
The peer index and memory namespace setup, base and reference controls, and
separate blind judgment run as part of that command.
