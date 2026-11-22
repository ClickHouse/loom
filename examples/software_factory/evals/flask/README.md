# Python E2E evaluation: Flask

The factory solves the same three public historical Flask issues across five
arms: no Loom, Tree-sitter, Tree-sitter with memory, SCIP, and SCIP with memory.
Claude chooses every Loom call. Source revisions, issue text, model, ordinary
source tools, resource limits and independent tests match across the arms.

These harder issues were selected before measuring their solver outcomes. Earlier
development results informed harness changes. Two tasks come from the pinned
SWE-bench dataset; callback-order task 4230 uses the original upstream issue and
regression test. The source and test patches remain unchanged.
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
Every arm receives the same final contract-review pass over its consolidated
candidate diff. Review calls and any subsequent revision remain in the measured
attempt; no hidden-test feedback enters the review.

## Historical knowledge

[historical_memories.json](memory/historical_memories.json) contains 30 short
project notes, including 14 code-linked notes. Public source URLs, content hashes,
publication and edit times, per-task cutoffs and excluded target discussions are
recorded beside the notes. Setup verifies source content before ingestion.
The facts concern prior architecture, compatibility and known pitfalls. The
corpus contains no reference fixes or hidden test requirements.

All four Loom arms have identical optional memory tools and private writable
namespaces. Only the two history-seeded arms receive prior facts; native namespace
listings certify that cold controls start empty. Facts are retrieved through
memory tools and code-linked lessons, with none inserted into prompts. Ordinary
project-note tools are disabled. This comparison measures the
combined contribution of historical knowledge and its native retrieval. It does
not isolate linking from the extra information; that needs a matched-facts ablation.

The history is replayed at the public 2.0.0rc2 revision, then the index advances
to each task's real buggy base. Native workers flag changed anchors. One memory
is revised from an independently published later document using a checked
version, and its link must become fresh again. This demonstrates a replayed
lifecycle; it does not measure autonomous learning over elapsed months. Each arm uses the same
current source revision as its paired code-only arm. No authored bug or synthetic
source revision is added to the task set.

## Verification and reporting

The model sees the public issue and can list, read and search the public
base-revision implementation, docs, examples and test sources. Only implementation
source is editable. It can run visible checks, including its own regression tests.
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
