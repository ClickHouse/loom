# Five-arm development pilot

One public callback-order issue runs once per arm with the same original source,
model, issue, hidden verifier and full regression checks. The serving source is
frozen at `f8eb6721e3d0f8e9f4edd7c5fa1065ee39b5458d`. Harbor uses its built-in
Claude Code agent, version `2.1.326`, with `claude-opus-5-5`. The complete numeric
export and original artifact hashes are in [results.json](results.json).

| Arm                  | Hidden solved | Blind fixed | Full-suite failures | Regressions | Provider USD | Agent seconds | Cost saving | Time saving |
| -------------------- | ------------: | ----------- | ------------------: | ----------: | -----------: | ------------: | ----------: | ----------: |
| No Loom              |           1/1 | Yes         |                   0 |           0 |    0.2213612 |     56.363912 |           — |           — |
| Tree-sitter          |           1/1 | Yes         |                   0 |           0 |    0.2041856 |     54.867726 |        7.8% |        2.7% |
| Tree-sitter + memory |           1/1 | Yes         |                   0 |           0 |    0.2860558 |     48.633809 |      -29.2% |       13.7% |
| SCIP                 |           0/1 | No          |                   1 |           0 |    0.2100052 |     55.998281 |        5.1% |        0.6% |
| SCIP + memory        |           1/1 | Yes         |                   0 |           0 |    0.1853962 |     45.435859 |       16.2% |       19.4% |

| Arm                  | Model responses | Tools | Loom calls | Output tokens | Total tokens | Agent test commands | Agent memory writes |
| -------------------- | --------------: | ----: | ---------: | ------------: | -----------: | ------------------: | ------------------: |
| No Loom              |              12 |    11 |          0 |         4,717 |      271,375 |                   4 |                   0 |
| Tree-sitter          |              10 |     9 |          2 |         4,393 |      279,281 |                   4 |                   0 |
| Tree-sitter + memory |               9 |     8 |          3 |         4,111 |      248,876 |                   2 |                   0 |
| SCIP                 |              12 |    11 |          3 |         4,011 |      335,033 |                   3 |                   0 |
| SCIP + memory        |               8 |     8 |          4 |         3,834 |      220,159 |                   2 |                   0 |

All solver costs reconcile to the native reported usage. Attributed Loom provider
charges are zero; local indexing, storage and compute are unpriced. Coding costs
total USD 1.107004. One separate blind judgment costs USD 0.815236. Model responses
come from the main CLI stream; tool totals use the native trajectory. Two initial
SCIP-memory tool calls share one response. Test commands count calls naming the test
runner; they do not count every process hidden inside scripts.

## Per-arm findings

- **No Loom:** the agent reads the early callbacks, later encounters a teardown
  failure in its own test, reads teardown, and fixes it. Extra test edits and
  verification produce twelve model responses.
- **Tree-sitter:** one successful regex lookup and one helper-definition lookup
  supply context. A local range read supplies the callback bodies. The agent fixes
  all four required functions and passes, with four test commands. Reduced cost
  and time fall short of the target.
- **Tree-sitter with memory:** the agent receives six lessons, totaling 1,364
  excerpt characters, once. It fixes all four functions with fewer responses and
  fewer generated tokens than baseline. Its initial prompt is cold: cache writes
  cost USD 0.161265 versus baseline's USD 0.076665. That difference explains the
  observed cost penalty; repeated lessons do not explain it in this attempt.
- **SCIP:** an invalid regex causes one rejected lookup and is corrected. The
  returned locations and the subsequent local read include teardown, but the
  candidate changes only the three early callback functions. Its own test does
  not cover teardown. The hidden callback-order test fails when application
  cleanup runs before child cleanup. This is not evidence of missing SCIP source.
  The remaining full-suite failure is present in the frozen base, so the separate
  regression count is zero.
- **SCIP with memory:** two initial literal lookups run together and do not find
  their exact patterns. A broader lookup returns the locations and six lessons.
  A local body read and helper lookup follow. The agent fixes all four functions,
  passes hidden and full checks, and uses eight model responses. It improves on
  SCIP's quality in this sample; the sample does not establish memory causality.

Every Loom arm uses a local read of unchanged indexed source despite the shared
project instruction. The result therefore does not establish efficient use of
Loom. No Loom arm calls the available batched enclosing-body tool. None has a
tool-discovery call in this pilot. Source and lessons arrive successfully; no
dropped-context defect is demonstrated.

## Cache and timing limits

Prompt-cache state differs between attempts. Keeping every later token unchanged,
the predeclared initial-input sensitivity places tree-sitter-memory at 3.9% above
baseline if both initial prompts are cold, or 6.7% below if both are warm. SCIP-memory
is 1.1% below under the cold sensitivity and 13.9% below under the warm sensitivity.
These are counterfactual supplements, not measurements under isolated cache states.
[Cost and time components](cost-time-analysis.json) retain the formulas' inputs and
native artifact hashes.

Reported API duration includes model, network and queuing time. CLI time outside
the API includes tool and client work; it cannot be assigned entirely to Loom.
Most of the observed time difference is in the API duration. A small reduction in
Loom server latency alone cannot explain the requested end-to-end saving.

## Decision and reproduction

No arm meets both 20% targets. The full matrix remains held. There is no A/A pair
or confidence interval in this one-scenario pilot. These are reused development
issues, not held-out confirmation. Preserve all outcomes and do not repeat
unchanged code to seek a favorable sample.

```bash
python3 -m examples.software_factory plan --pilot-task pallets__flask-4230
ANTHROPIC_API_KEY=... python3 -m examples.software_factory run --pilot-task pallets__flask-4230 --output ./factory-pilot
```

The [findings and decisions](../../LEARNINGS.md) distinguish merged fixes,
interface improvements to validate, measurement gaps and solver mistakes. Hidden
failures remain evaluator evidence and must not become issue hints or historical
memory entries.
