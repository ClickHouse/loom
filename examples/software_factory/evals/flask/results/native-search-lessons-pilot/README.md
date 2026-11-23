# Native search lessons pilot

All five attempts miss the hidden grade. Each Loom arm spends more and takes
longer than the no-Loom attempt. Tree-sitter with memory passes one additional
hidden check and still leaves one failing check. None introduces a full-suite
regression.

This pilot uses the frozen multi-mount blueprint issue `pallets__flask-4074`.
The task was selected before inference because six retained searches returned
lessons linked to both the requested blueprint registration method and other
methods in the same file. The public issue, source, grading, memory corpus and
conditional project guidance come from the existing native cohort. Both
base/reference controls pass with their expected rewards of zero and one.
Harbor uses native Claude Code 2.1.326 with `claude-opus-5-5`.
The serving revision is [3cd2138b6ca2a1719565a95bdb2c8bd1a8048026](https://github.com/ClickHouse/loom/tree/3cd2138b6ca2a1719565a95bdb2c8bd1a8048026).
[Exported measurements](results.json)

| Arm                  | Hidden solved | Blind fixed | Full-suite failures | Regressions | Provider USD | Agent seconds | End-to-end seconds |
| -------------------- | ------------: | ----------- | ------------------: | ----------: | -----------: | ------------: | -----------------: |
| No Loom              |           0/1 | Yes         |                   2 |           0 |    0.2781446 |     52.951253 |             68.235 |
| Tree-sitter          |           0/1 | Yes         |                   2 |           0 |    0.3899576 |     69.254431 |             87.337 |
| Tree-sitter + memory |           0/1 | No          |                   1 |           0 |    0.4704366 |       104.787 |            123.669 |
| SCIP                 |           0/1 | Yes         |                   2 |           0 |    0.3255006 |     73.248110 |             91.789 |
| SCIP + memory        |           0/1 | Yes         |                   2 |           0 |    0.3211460 |     71.207551 |             89.667 |

| Arm                  | Model responses | Total tokens | Tools | Loom calls | Agent test commands | Agent memory writes |
| -------------------- | --------------: | -----------: | ----: | ---------: | ------------------: | ------------------: |
| No Loom              |              10 |      239,807 |    10 |          0 |                   1 |                   0 |
| Tree-sitter          |              12 |      392,830 |    14 |          7 |                   1 |                   0 |
| Tree-sitter + memory |              19 |      753,391 |    20 |          8 |                   5 |                   0 |
| SCIP                 |              14 |      461,567 |    13 |          5 |                   1 |                   0 |
| SCIP + memory        |              11 |      392,724 |    11 |          4 |                   1 |                   0 |

Solver provider cost totals USD 1.7851854. Attributed Loom provider charges
are zero. The blind judgment costs USD 1.130216 separately. Local
infrastructure and embedding are unpriced.

## Quality and historical memory

Each submitted implementation adds distinct registration names for repeat
mounts. The hidden grader also checks duplicate-name warnings and rejects
direct self-registration. All five implementations miss the self-registration
check. Only Tree-sitter with memory passes the duplicate-name warning check.
The blind judge rates the issue text independently. It accepts the four patches with the narrower registration change and rejects
the Tree-sitter with memory patch. Its reasons include warnings for repeated
registration under the same name and changes to nested blueprint keys. Those
concerns did not introduce failures measured by the frozen full-suite check.

Both memory arms receive the registration lesson attached to
`Flask.register_blueprint`. Each first `code.search` omits five lessons
linked to other methods. SCIP with memory receives four retained lessons
and Tree-sitter with memory receives five, including lessons attached
to its other returned files. Across all calls, the scoped search and grep
responses omit 17 lesson rows for SCIP with memory and six for Tree-sitter
with memory. The totals sum per-response omissions, so consecutive calls
can count the same omitted lesson. Returned histories include both fresh
and changed link states. The benchmark measures access to both the history
and its code links.

## Where cost and time went

All 24 Loom calls are direct tool calls. Two fail validation. SCIP sends an
RE2 backreference and retries with a literal list. SCIP with memory changes
its search while reusing the earlier cursor and retries without it. Three
arms use the literal-list form of grep, including both SCIP configurations
and Tree-sitter with memory.

| Arm                  | Model API seconds | Loom request sum (ms) | First-prompt cache writes | First-prompt cache reads |
| -------------------- | ----------------: | --------------------: | ------------------------: | -----------------------: |
| No Loom              |            47.833 |                     0 |                    17,596 |                        0 |
| Tree-sitter          |            63.944 |                   266 |                    22,576 |                        0 |
| Tree-sitter + memory |            94.705 |                   544 |                     4,477 |                   18,098 |
| SCIP                 |            68.030 |                    95 |                     4,478 |                   18,098 |
| SCIP + memory        |            66.143 |                   283 |                     4,477 |                   18,098 |

The API duration includes model, network and queue time. The Loom column
sums transport ledger durations, including stages after the code handler.
Requests can overlap. Across the five attempts, the differences in API
duration account for most of the agent-time gap against baseline.

- **No Loom** uses one unsuccessful tool discovery call for Loom, then
  reads source locally. Its conditional project guidance is identical to
  the other arms.
- **Tree-sitter** receives five source bodies in the first search and
  makes six more Loom calls. It uses two more model responses than
  baseline. Its cost is 40.2% higher and agent time is 30.8% longer.
- **Tree-sitter + memory** takes 19 responses and five test commands.
  It performs additional inspection and edits around name collisions and
  request routing. It uses 753,391 total tokens versus 392,830 for
  Tree-sitter alone, despite a partially warm first prompt. Against
  baseline it costs 69.1% more and takes 97.9% longer.
- **SCIP** uses five Loom grep calls. It reads implementation bodies
  locally after two successful indexed greps that return lines. Its cost
  is 17.0% higher and agent time is 38.3% longer than baseline.
- **SCIP + memory** uses 68,843 fewer tokens and three fewer model
  responses than SCIP alone. It is 1.3% cheaper and 2.8% faster than
  SCIP alone, with the same grade. Against baseline it costs 15.5% more
  and takes 34.5% longer.

First-prompt usage is recorded before any Loom tool returns a result.
Replacing only that prompt's charging with the same cold-write or
warm-read rule for all arms leaves every Loom arm more expensive than
baseline. The cost table uses observed provider usage and prices.

## Decision and reproduction

Keep the full matrix on hold. This single issue and repeat provides direct
evidence that scoped lessons are delivered and omitted as intended. It
does not establish repeatable quality or efficiency gains. Use the frozen
revision and [factory instructions](../../REPRODUCE.md) with
`--pilot-task pallets__flask-4074`.
