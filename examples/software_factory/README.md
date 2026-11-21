# Software factory evaluations

The shared coding factory runs with ordinary source tools or optional read-only
Loom tools. Harbor owns trial environments, verification, reporting and charts.
Each repository evaluation pins its public issues, source revisions, dependencies,
index scopes and test adapters in its own directory.

## Python E2E evaluation: Flask

[Run the Flask evaluation](evals/flask/README.md) and follow its
[reproduction instructions](evals/flask/REPRODUCE.md). The suite compares baseline,
Claude-selected Loom Tree-sitter and Claude-selected Loom SCIP on the same public
source. No enforced Loom path exists.

The layout separates the agent and evaluation machinery from repository choices:

- `factory/`: shared loop, source tools, grounding and provider/SDK adapters.
- `harbor/`: native runner, frozen protocols, accounting, reports, charts and export.
- `evals/flask/`: task/dependency lock, setup, source and pytest adapters, and controls.
- `tests/`: factory, Harbor and Flask tests; authored Python smoke fixtures are test
  data rather than repository benchmarks.

A repository suite declares `languages` and `source_roots` in its lock, so a
future suite can cover several languages in one issue and one end-to-end result.
Only the Flask/Python adapter is implemented here.

Run the free authored smoke checks from the repository root:

```bash
python -m examples.software_factory.tests.smoke smoke --output /tmp/factory-smoke
python -m unittest discover -s examples/software_factory/tests -t . -v
```

The smoke uses scripted actions and an expression grader. It makes no model or
network calls and measures no model quality, Loom adoption or cost savings.
