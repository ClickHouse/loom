# Flask evaluation results

The [implementation-scope cohort](historical-issues-implementation-scope/README.md)
contains sanitized measurements for all 51 attempts and their frozen input
provenance. It includes 45 primary attempts and six baseline A/A controls on three
public historical issues. Twelve blind comparisons supply a separate fixed rating.
Earlier development results informed the harness; these tasks were selected before
their solver outcomes. Three task clusters cannot establish generalization.

| Treatment | Hidden solved | Blind fixed | Provider USD | Mean agent seconds | Mean E2E seconds | Loom calls |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| No Loom | 3/9 | 3/9 | 0.2273954 | 43.616 | 59.965 | 0 |
| Tree-sitter | 3/9 | 3/9 | 0.1918547 | 41.167 | 59.559 | 55 |
| Tree-sitter and memory | 3/9 | 6/9 | 0.2911961 | 51.125 | 69.984 | 65 |
| SCIP | 2/9 | 4/9 | 0.1813363 | 37.362 | 55.797 | 42 |
| SCIP and memory | 3/9 | 5/9 | 0.2217326 | 45.358 | 64.135 | 51 |

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

The bundle includes [standalone charts](historical-issues-implementation-scope/charts/reports/charts.html),
[attempt measurements](historical-issues-implementation-scope/attempts.json),
[protocol](historical-issues-implementation-scope/protocol.json) and
[provenance](historical-issues-implementation-scope/provenance.json).
Its manifest verifies the published bytes. Private native trajectories, credentials
and raw billing ledger rows are outside this bundle; the exporter validates raw
evidence before publishing scalar results and hashes. Reproduction writes a fresh
private artifact set to the selected output directory.
