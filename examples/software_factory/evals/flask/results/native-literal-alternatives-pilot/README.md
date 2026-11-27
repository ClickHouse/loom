# Native literal-alternatives pilot

All five attempts solve the callback-order issue and pass the full suite. The
blind judge marks every patch fixed. Both SCIP configurations spend at least 20%
less and finish at least 20% sooner in agent time. The two memory configurations
tie their code-only peers on quality.

The run uses the same issue, grading, model, arm order and memory corpus as the
[grep body pilot](../native-grep-body-pilot/README.md). The only product change
between frozen serving revisions is a literal-list option for `code.grep`.
Harbor uses native Claude Code 2.1.326 with `claude-opus-5-5`. The serving
revision is [a142da69ecc753c57ccef145fba862e3191ea8a4](https://github.com/ClickHouse/loom/tree/a142da69ecc753c57ccef145fba862e3191ea8a4).
[Exported measurements](results.json)

| Arm                  | Hidden solved | Blind fixed | Full-suite failures | Regressions | Provider USD | Agent seconds | End-to-end seconds |
| -------------------- | ------------: | ----------- | ------------------: | ----------: | -----------: | ------------: | -----------------: |
| No Loom              |           1/1 | Yes         |                   0 |           0 |    0.2641726 |     66.588013 |             82.408 |
| Tree-sitter          |           1/1 | Yes         |                   0 |           0 |    0.2514568 |     65.050078 |             83.371 |
| Tree-sitter + memory |           1/1 | Yes         |                   0 |           0 |    0.2985540 |     48.022956 |             66.384 |
| SCIP                 |           1/1 | Yes         |                   0 |           0 |    0.2092108 |     48.357868 |             67.014 |
| SCIP + memory        |           1/1 | Yes         |                   0 |           0 |    0.1775630 |     40.807157 |             59.730 |

| Arm                  | Model responses | Total tokens | Tools | Loom calls | Agent test commands | Agent memory writes |
| -------------------- | --------------: | -----------: | ----: | ---------: | ------------------: | ------------------: |
| No Loom              |              12 |      299,699 |    11 |          0 |                   2 |                   0 |
| Tree-sitter          |              13 |      368,247 |    12 |          4 |                   5 |                   0 |
| Tree-sitter + memory |               9 |      268,740 |     9 |          4 |                   2 |                   0 |
| SCIP                 |              11 |      308,746 |    11 |          3 |                   2 |                   0 |
| SCIP + memory        |               9 |      255,343 |     8 |          3 |                   2 |                   0 |

SCIP saves 20.8% on provider cost, 27.4% on agent time and 18.7% on full
end-to-end time. SCIP with memory saves 32.8%, 38.7% and 27.5%, respectively.
Solver provider cost totals USD 1.2009572. The separate blind judgment costs
USD 0.81978. Measured provider cost totals USD 2.0207372. Attributed Loom
provider charges are zero. Local infrastructure and embedding are unpriced.

## Where the time and cost went

| Arm                  | Model API seconds | CLI outside API seconds | Harbor outside CLI seconds | Loom server request sum (ms) |
| -------------------- | ----------------: | ----------------------: | -------------------------: | ---------------------------: |
| No Loom              |            60.844 |                   2.871 |                      2.873 |                            0 |
| Tree-sitter          |            55.755 |                   6.271 |                      3.024 |                           62 |
| Tree-sitter + memory |            42.344 |                   2.598 |                      3.081 |                          248 |
| SCIP                 |            42.558 |                   2.825 |                      2.975 |                           62 |
| SCIP + memory        |            35.592 |                   2.278 |                      2.937 |                          106 |

Agent time is the sum of the first three durations. The API duration includes
network and queue time. CLI time outside the API includes tool execution and
client work. The last column sums server requests, which can overlap.

- **No Loom** reads the relevant source locally, changes four callback functions
  including teardown, and verifies the result with two test commands.
- **Tree-sitter** asks for bodies on an empty first lookup. It gets seven lines
  on its second call, then reads the callback bodies locally. It makes five
  test commands and thirteen model responses, one more response than baseline.
  Cost falls 4.8% and agent time falls 2.3%. Full end-to-end time rises 1.2%.
  It uses 362,986 input tokens versus 293,535 for baseline. Lower output and
  cache-write charges offset higher cache-read charges in the observed total.
- **Tree-sitter + memory** gets six callback bodies and one 243-character linked
  lesson in the same successful grep. The agent skips a separate read of those
  bodies. Agent time falls 27.9% while cost rises 13.0%. Cache writes cost
  USD 0.08032 more than baseline. The other token components together cost
  USD 0.0459386 less.
- **SCIP** makes three grep calls, starting with an empty exact search and
  finishing with a valid alternation. It reads indexed source locally and
  spends eleven model responses on the fix.
- **SCIP + memory** first sends an invalid RE2 expression, then receives six
  callback bodies and one 243-character linked lesson in the next response.
  It skips a local read of those bodies. The CLI reports 35.592 seconds in the
  model API versus 60.844 seconds for baseline. That difference accounts for
  most of the observed agent-time gap.

Claude sends one invalid regex in this pilot, versus three in the preceding
pilot. All requests use a string for `pattern`. The literal-list route is never
selected, so this comparison supplies no measurement of its effect on retry
rates or savings.

The two Tree-sitter first prompts contain 22,673 and 22,674 tokens with and
without memory, respectively. The memory prompt writes 22,671 cache tokens and
reads zero. The other writes 4,478 tokens and reads 18,194. Both first inputs
precede the first Loom response. Across the attempts, the memory arm spends
USD 0.08923 more on cache writes and USD 0.0421328 less on other token
components. Its observed cost is USD 0.0470972 above Tree-sitter alone.
The first two grep calls in the memory attempt share one model response.

The first-input cache sensitivity keeps every later token as recorded. Under
equal cold first prompts, SCIP with memory costs 16.7% less than
baseline and SCIP costs 6.8% less. Under equal warm first prompts their
reductions are 33.1% and 19.5%, respectively. The observed charges are the
figures in the table.

Against Tree-sitter alone, the same first-input sensitivity puts Tree-sitter
with memory 11.9% lower with both first prompts cold and 17.5% lower with both
warm. These counterfactuals leave later cache usage and all model actions as
recorded.

## Decision and reproduction

Keep the full matrix on hold. The Tree-sitter configurations miss the combined
efficiency target. Every candidate makes the teardown change required by the
hidden test. Equal quality across all five attempts gives no evidence of higher
quality with historical memory in this sample. One issue and one attempt per
arm cannot establish repeatable savings or quality differences across issues.

Use the frozen revision and the
[factory instructions](../../REPRODUCE.md) with `--pilot-task pallets__flask-4230`.
