# Python E2E evaluation: Flask

The factory solves the same three public historical Flask issues across five
arms: no Loom, Tree-sitter, Tree-sitter with memory, SCIP, and SCIP with memory.
Claude chooses every Loom call. Source revisions, issue text, model, ordinary
source tools, resource limits and independent tests match across the arms.

This is a development regression study: earlier outcomes on these issues were
inspected. It does not establish performance on unseen repository tasks. The
pinned SWE-bench test dataset contains eleven Flask issues; this pilot uses three.
Three task clusters and repeated attempts cannot establish a general Flask win.

## Run

Follow [REPRODUCE.md](REPRODUCE.md). From the repository root with Python 3.12,
a local Docker daemon and an Anthropic API credential:

```bash
python3 -m examples.software_factory plan --eval flask
ANTHROPIC_API_KEY=... python3 -m examples.software_factory run --eval flask --output ./factory-run
```

The launcher prepares isolated services, source indexes, dependency images and
verifiers. Three repeats across five arms produce 45 primary attempts. Six
separate baseline A/A controls produce 51 total attempts. Twelve blind fixed-only
judgments use the same Haiku 5.5 model. Retries are disabled. Every attempt is fresh.

## Historical knowledge

[historical_memories.json](memory/historical_memories.json) contains 24 short
project notes, including ten code-linked notes. Public source URLs, content hashes,
publication and edit times, per-task cutoffs and excluded target discussions are
recorded beside the notes. Setup verifies source content before ingestion.
The facts concern prior architecture, compatibility and known pitfalls. The
corpus contains no reference fixes or hidden test requirements.

Only memory-enabled arms receive this historical knowledge, through native memory
tools and code-linked lessons. It is not inserted into the prompt. Other arms have
no seeded history or ordinary project-note tools. This comparison measures the
combined contribution of historical knowledge and its native retrieval. It does
not isolate linking from the extra information; that needs a matched-facts ablation.

The history is curated and replayed into fresh namespaces. This study does not
measure autonomous learning over elapsed months. Each memory arm uses the same
current source revision as its paired code-only arm. No authored bug or synthetic
source revision is added to the task set.

## Verification and reporting

The model sees the public issue and allowlisted implementation source. It can
read, search, edit and run visible checks, including its own regression tests.
Reference patches, Git objects and evaluator inputs stay outside its tools.
The independent hidden verifier decides success. Full-suite regressions and the
blind judge are separate measurements; authored tests do not establish success.

[Harbor reports](../../harbor/README.md) retain hidden success, blind fixed judgments,
full-suite regressions, agent and end-to-end time, agent plus Loom provider cost,
tokens, test runs, Loom selections and persisted memory writes. Setup, judging and
unpriced infrastructure are reported separately. Failed attempts remain included;
unknown values remain unknown. Zero optional Loom calls is valid.

The targets are 9/9 hidden successes per arm and at least 20% lower agent time and
provider cost than the paired baseline. They are hypotheses, not measured claims.
Read [PILOT.md](PILOT.md) for the comparison policy and
[the architecture](../../../../docs/architecture/software-factory-evaluation.md)
for the sandbox and source boundaries.
