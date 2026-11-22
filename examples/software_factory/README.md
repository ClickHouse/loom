# Software factory evaluations

From the repository root, inspect the evaluation without installing Python packages
or contacting a provider:

```bash
python3 -m examples.software_factory plan
```

Run with a local Docker daemon:

```bash
ANTHROPIC_API_KEY=... python3 -m examples.software_factory run --output ./factory-run
```

The launcher builds pinned controller dependencies, mounts source read-only, and
automates the inner setup and evaluation. The plan contains three historical issues, five arms,
three repeats and six separate A/A controls: 51 attempts, plus 12 blind
fixed-only judgments using the same model. Embedding uses the checked local
`bge-small` weights. The plan reports Loom's separate native Anthropic model
default; all provider requests contribute to the cost ledger. Runs have no dollar cap
unless `--budget-usd` is supplied. Memory arms expose 30 short historical project records through native tools;
14 are linked to code. The two code-only arms disable memory tools and linked-memory
enrichment; they receive no memory namespace or historical notes.

Interrupted work is never retried automatically. Explicit resume preserves the
frozen source and budget, and the inner controller refuses unsettled paid requests:

```bash
ANTHROPIC_API_KEY=... python3 -m examples.software_factory resume --output ./factory-run
python3 -m examples.software_factory cleanup --output ./factory-run
```

Cleanup uses recorded resource identities and ownership labels. It retains local
reports and receipts; it does not prune unrelated Docker data.

Use `--eval flask` on any command to select the published profile explicitly; it
is the default. Each `evals/<id>/eval.json` names that project's public suite,
scenarios, memory catalog and adapter. Profiles supply facts, build and index
recipes, and tests; the Harbor runner, metrics, budget gateway and reports are
shared. Resume and cleanup retain the selected profile identity.

Every arm has ordinary source tools. Model-selected Loom
tools add Tree-sitter or SCIP retrieval. Only the two memory arms expose native memory
tools and code-linked historical lessons.
Harbor owns trial environments, verification, reporting and charts.
Each repository evaluation pins its public issues, source revisions, dependencies,
index scopes and test adapters in its own directory.

## Python E2E evaluation: Flask

[Run the Flask evaluation](evals/flask/README.md) and follow its
[reproduction instructions](evals/flask/REPRODUCE.md). The five arms compare baseline,
model-selected Tree-sitter and SCIP retrieval, and each retrieval tier with native
memory. All arms solve the same case source; no enforced Loom path exists.

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

The private attempt ledger retains each authorized native code response beside the
projected context given to the model. This makes omitted navigation or source context
inspectable without another retrieval call. Member outlines and descriptions remain
navigation metadata; only bodies matching the working source are treated as code.
Public exports include allowlisted measurements and provenance hashes rather than
these raw response bodies. Retrieval requests inherit Loom's response token defaults.
