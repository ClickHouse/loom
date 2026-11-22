# Flask evaluation results

The [public-evidence cohort](historical-issues-public-evidence-review/README.md)
contains all 51 attempts: 45 primary attempts and six baseline A/A controls on three
historical issues. Twelve blind comparisons supply separate fixed ratings. Every
arm can read the public base-revision docs and tests and receives one final review
of its candidate. All review requests, tokens, time and cost are included.

| Treatment              | Hidden solved | Blind fixed | Provider USD | Mean agent seconds | Mean E2E seconds | Loom calls |
| ---------------------- | ------------: | ----------: | -----------: | -----------------: | ---------------: | ---------: |
| No Loom                |           3/9 |         4/9 |    0.3129294 |             59.149 |           77.679 |          0 |
| Tree-sitter            |           4/9 |         5/9 |    0.3989008 |             65.925 |           86.736 |         31 |
| Tree-sitter and memory |           3/9 |         4/9 |    0.4474063 |             64.814 |           85.851 |         44 |
| SCIP                   |           3/9 |         3/9 |    0.3803920 |             64.273 |           84.965 |         38 |
| SCIP and memory        |           4/9 |         3/9 |    0.4117454 |             63.967 |           85.034 |         28 |

Every Loom arm costs more and takes longer than baseline. Memory increases SCIP
hidden success by one attempt and reduces Tree-sitter success by one. No arm meets
the combined quality, cost and time target. All primary attempts have zero
full-suite regressions and no solver writes memory. Only three task clusters and
large baseline A/A variation limit conclusions about treatment effects.

All 375 native requests match scoped ledger evidence. Native model provider cost
is zero; local embedding and infrastructure resources have no monetary price
assigned. Costs are estimates from recorded provider usage and pinned prices,
not invoices. Coding including A/A costs USD 2.2008896; judging costs USD 0.4134824;
the total is USD 2.6143720.

This cohort executes
[frozen source](https://github.com/ClickHouse/loom/tree/6951accfcbfe9f238960b411591101cd7a992676).
Follow that revision's
[reproduction instructions](https://github.com/ClickHouse/loom/blob/6951accfcbfe9f238960b411591101cd7a992676/examples/software_factory/evals/flask/REPRODUCE.md)
to repeat its method. The 30 historical facts include 13 code links. Cross-file
lesson ranking, enclosing-code lessons and adapter corrections in the prepared
source are outside this measured cohort. Model outputs, latency and cost can vary.

The bundle includes [standalone charts](historical-issues-public-evidence-review/charts/reports/charts.html),
[attempt measurements](historical-issues-public-evidence-review/attempts.json),
[protocol](historical-issues-public-evidence-review/protocol.json) and
[provenance](historical-issues-public-evidence-review/provenance.json).
Its offline export corrects two locally refused tool selections to zero provider
cost using verified zero-dispatch evidence. Selected-call counts remain one each;
source, patches, outcomes, timing and provider usage are unchanged.

The [implementation-scope cohort](historical-issues-implementation-scope/README.md)
contains sanitized measurements for all 51 attempts and their frozen input
provenance. It includes 45 primary attempts and six baseline A/A controls on three
public historical issues. Twelve blind comparisons supply a separate fixed rating.
Earlier development results informed the harness; these tasks were selected before
their solver outcomes. Three task clusters cannot establish generalization.

| Treatment              | Hidden solved | Blind fixed | Provider USD | Mean agent seconds | Mean E2E seconds | Loom calls |
| ---------------------- | ------------: | ----------: | -----------: | -----------------: | ---------------: | ---------: |
| No Loom                |           3/9 |         3/9 |    0.2273954 |             43.616 |           59.965 |          0 |
| Tree-sitter            |           3/9 |         3/9 |    0.1918547 |             41.167 |           59.559 |         55 |
| Tree-sitter and memory |           3/9 |         6/9 |    0.2911961 |             51.125 |           69.984 |         65 |
| SCIP                   |           2/9 |         4/9 |    0.1813363 |             37.362 |           55.797 |         42 |
| SCIP and memory        |           3/9 |         5/9 |    0.2217326 |             45.358 |           64.135 |         51 |

No arm meets the combined quality, cost and time target. SCIP has one full-suite
regression; the other primary arms have none. No solver writes memory.
Provider cost includes coding and native model charges; setup and judging are
separate. Infrastructure has no monetary price assigned. Including A/A controls
and judging, total provider spend is USD 1.8624293, of which USD 0.6420386 is judging.

This cohort executes
[frozen source](https://github.com/ClickHouse/loom/tree/3479c03a6494f59e9056f3666830a0caf3859b4f).
Its agents can read implementation source only. Public repository evidence,
query-aware lesson ranking and the final contract-review pass require a fresh cohort;
they did not alter these results. Follow that revision's
[reproduction instructions](https://github.com/ClickHouse/loom/blob/3479c03a6494f59e9056f3666830a0caf3859b4f/examples/software_factory/evals/flask/REPRODUCE.md)
to repeat its method. Model outputs, latency and cost can vary.

That bundle includes [standalone charts](historical-issues-implementation-scope/charts/reports/charts.html),
[attempt measurements](historical-issues-implementation-scope/attempts.json),
[protocol](historical-issues-implementation-scope/protocol.json) and
[provenance](historical-issues-implementation-scope/provenance.json).
Its manifest verifies the published bytes. Private native trajectories, credentials
and raw billing ledger rows are outside this bundle; the exporter validates raw
evidence before publishing scalar results and hashes. Reproduction writes a fresh
private artifact set to the selected output directory.

The published protocol and chart JSON use repository formatting; their parsed
values are identical to the recorded export. The manifest hashes these published
bytes. The original export remains available in
[the publication commit](https://github.com/ClickHouse/loom/tree/5c53ac7c64c65410b0eecfcd9616a725b2c2ade9/examples/software_factory/evals/flask/results/historical-issues-implementation-scope).
