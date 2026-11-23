# Native Flask CLI option-order pilot

All five attempts pass the hidden tests, full suite and blind grade. Tree-sitter
and SCIP with memory meet the 20% cost and agent-time targets against baseline
using observed provider usage. The memory arms tie their code-only peers on
quality.

The pilot selects `pallets__flask-4544` from the pinned SWE-bench dataset. No attempt
on this issue appears in the 32 retained manifests inspected before inference.
The original source, issue, reference and tests are locked. The base and
reference controls earn the expected rewards of zero and one. The two memory
arms receive the same corpus of 31 historical notes, including an independently
dated dependency note linked to the Flask option validator. The native agent
and blind judge both use `claude-opus-5-5`. [Original issue](https://github.com/pallets/flask/issues/4459)
[Published measurements](results.json)

| Arm                  | Hidden solved | Blind fixed | Regressions | Provider USD | Agent seconds | End-to-end seconds |
| -------------------- | ------------: | ----------- | ----------: | -----------: | ------------: | -----------------: |
| No Loom              |           1/1 | Yes         |           0 |    0.1831500 |     35.734343 |             50.605 |
| Tree-sitter          |           1/1 | Yes         |           0 |    0.1049974 |     24.507773 |             42.526 |
| Tree-sitter + memory |           1/1 | Yes         |           0 |    0.1270346 |     30.884266 |             48.753 |
| SCIP                 |           1/1 | Yes         |           0 |    0.2127680 |     28.953799 |             46.962 |
| SCIP + memory        |           1/1 | Yes         |           0 |    0.0922794 |     19.948860 |             37.996 |

| Arm                  | Model responses | Total tokens | Tools | Loom calls | Agent test commands | Agent memory writes |
| -------------------- | --------------: | -----------: | ----: | ---------: | ------------------: | ------------------: |
| No Loom              |               8 |      167,603 |     8 |          0 |                   2 |                   0 |
| Tree-sitter          |               7 |      172,380 |     8 |          2 |                   1 |                   0 |
| Tree-sitter + memory |               9 |      228,112 |     9 |          3 |                   1 |                   0 |
| SCIP                 |               9 |      231,706 |     9 |          2 |                   2 |                   0 |
| SCIP + memory        |               5 |      123,896 |     4 |          1 |                   1 |                   0 |

The solver provider total is USD 0.7202294. All eight Loom calls succeed and
incur zero attributed backend provider charges. The separate blind judgment
costs USD 0.344972. Local infrastructure and embeddings are unpriced.

## What the agent used

Both memory arms receive the fresh callback-order note inside the first
`code.symbol` response, before editing. SCIP with memory uses that one Loom
call. It reads the option declarations and tests locally, then edits and
runs tests. Its 19.949 seconds include 16.301 seconds reported by the model
API and a 43 ms Loom request.

Tree-sitter with memory receives the note again in a grep result. Replaying
the recorded stream with whitespace-normalized excerpt identities counts
282 repeated characters. The frozen measurement uses whole-row equality and
reports zero for that field. Two response shapes differ in formatting and
their fields. The published raw measurements preserve the frozen values.

Both code-only Loom arms grep for `_validate_key` with bodies enabled. Loom
returns the complete validator and also matches its use in a multiline
decorator. The decorator hit lacks an enclosing body. Both agents follow
with a local read of the decorator area. A later grep returns the test
definition.

## Cost and time

| Arm                  | Observed cost change | Agent-time change | Equal cold first-input cost change | Equal warm first-input cost change |
| -------------------- | -------------------: | ----------------: | ---------------------------------: | ---------------------------------: |
| Tree-sitter          |               −42.7% |            −31.4% |                              +4.1% |                             −15.4% |
| Tree-sitter + memory |               −30.6% |            −13.6% |                             +16.1% |                              +7.0% |
| SCIP                 |               +16.2% |            −19.0% |                             +16.2% |                              +7.1% |
| SCIP + memory        |               −49.6% |            −44.2% |                              −2.9% |                             −28.3% |

The first-input sensitivity replaces the charge for the first prompt only.
It retains later calls, usage and prices. The baseline and SCIP first prompts
have zero cache reads. The other three arms each read 17,833 initial tokens
from cache. This affects whether an observed cost gain meets the target.
The measured provider costs are the values in the first table.

SCIP with memory uses five model responses and 123,896 total tokens.
Baseline uses eight and 167,603. Tree-sitter with memory uses nine responses
and 228,112 tokens, two responses and 55,732 tokens above Tree-sitter
alone. Loom server request sums range from 43 to 239 ms. The model API
duration includes model, queue and network time.

## Reproduction

The pilot uses the [frozen CLI order revision](https://github.com/ClickHouse/loom/tree/f2867fd00f53660ea28ce99cd03f4ebc0b43b1ab).
Follow its [CLI pilot instructions](https://github.com/ClickHouse/loom/blob/f2867fd00f53660ea28ce99cd03f4ebc0b43b1ab/examples/software_factory/evals/flask/CLI-ORDER-PILOT.md).
This five-attempt pilot has no A/A pair or repeat. The original three-issue
matrix is the gate for the full benchmark and remains on hold.
