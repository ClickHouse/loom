# Native Flask CLI cache-pair pilot

All ten attempts pass the hidden tests and the blind judge. All five cache
pairs verify. No arm reaches 20% lower cost in either cache condition.
SCIP with historical memory reaches 44.9% lower agent time and 24.0% lower
end-to-end time on the cold start. It saves 3.4% in provider cost. Warm
No Loom is the least expensive arm. The issue produces a quality tie.

This one-issue pilot uses `pallets__flask-4544` in all five arms with native
Claude Code 2.1.326 and Claude Opus 5.5. Each Claude session starts fresh.
Each warm attempt follows its own cold attempt in an isolated provider
workspace. The controller waits five minutes before starting the next
arm. The frozen base and upstream reference earn rewards of zero and one.
Regressions and solver memory writes are zero across the ten attempts.

Each percentage compares against No Loom with the same starting cache
condition. Agent time covers the native coding loop. End-to-end time also
includes the trial setup, verifier and index audit. The quiet periods are
outside both timers. Solver costs come from Claude Code. They total
$1.3563648. The two blind judge requests cost $0.686484 separately. The Loom
request ledger attributes zero upstream provider cost. Infrastructure and
index preparation are unpriced.

[Observed metrics and verified input hashes](results.json)

## Cold provider cache

| Arm | Hidden | Blind | Cost USD | Cost change | Agent s | Agent change | End-to-end s | End-to-end change | Loom / total calls |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| No Loom | 1/1 | 1/1 | 0.1800088 | +0.0% | 37.48 | +0.0% | 52.95 | +0.0% | 0 / 7 |
| Tree-sitter | 1/1 | 1/1 | 0.1923018 | +6.8% | 25.90 | -30.9% | 44.34 | -16.3% | 2 / 5 |
| Tree-sitter + memory | 1/1 | 1/1 | 0.1933944 | +7.4% | 24.47 | -34.7% | 43.83 | -17.2% | 3 / 8 |
| SCIP | 1/1 | 1/1 | 0.2030858 | +12.8% | 30.25 | -19.3% | 49.86 | -5.8% | 1 / 8 |
| SCIP + memory | 1/1 | 1/1 | 0.1739470 | -3.4% | 20.66 | -44.9% | 40.27 | -24.0% | 1 / 4 |

## Warm provider cache

| Arm | Hidden | Blind | Cost USD | Cost change | Agent s | Agent change | End-to-end s | End-to-end change | Loom / total calls |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| No Loom | 1/1 | 1/1 | 0.0659390 | +0.0% | 24.15 | +0.0% | 39.35 | +0.0% | 0 / 5 |
| Tree-sitter | 1/1 | 1/1 | 0.0792072 | +20.1% | 22.86 | -5.3% | 40.88 | +3.9% | 2 / 7 |
| Tree-sitter + memory | 1/1 | 1/1 | 0.1021598 | +54.9% | 29.93 | +23.9% | 48.73 | +23.8% | 2 / 9 |
| SCIP | 1/1 | 1/1 | 0.0759168 | +15.1% | 24.02 | -0.5% | 43.19 | +9.8% | 1 / 5 |
| SCIP + memory | 1/1 | 1/1 | 0.0904042 | +37.1% | 26.15 | +8.3% | 44.67 | +13.5% | 3 / 7 |

## First-prompt cache and solver work

| Arm | Start | First reads | First writes | First response USD | Later responses USD | Model responses | Solver tokens |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |
| SCIP | cold | 0 | 22,350 | 0.1132180 | 0.0898678 | 9 | 221,596 |
| SCIP | warm | 22,350 | 0 | 0.0064380 | 0.0694788 | 6 | 147,603 |
| Tree-sitter + memory | cold | 0 | 22,339 | 0.1158030 | 0.0775914 | 6 | 153,678 |
| Tree-sitter + memory | warm | 22,339 | 0 | 0.0059358 | 0.0962240 | 8 | 201,330 |
| SCIP + memory | cold | 0 | 22,339 | 0.1131630 | 0.0607840 | 5 | 121,753 |
| SCIP + memory | warm | 22,339 | 0 | 0.0059358 | 0.0844684 | 7 | 176,463 |
| No Loom | cold | 0 | 17,624 | 0.0918680 | 0.0881408 | 8 | 165,127 |
| No Loom | warm | 17,624 | 0 | 0.0075928 | 0.0583462 | 6 | 117,550 |
| Tree-sitter | cold | 0 | 22,352 | 0.1135080 | 0.0787938 | 6 | 153,186 |
| Tree-sitter | warm | 22,352 | 0 | 0.0062184 | 0.0729888 | 5 | 127,538 |

5 / 5 planned first-prompt cache pairs match exactly.

The token counts reconcile to Anthropic's usage in the native stream for all
ten attempts. The first-response and later-response costs use the native
trajectory's response estimates and reconcile to the reported solver totals.
The machine-readable export includes uncached input, cache reads, five-minute
and one-hour cache writes, and output for every attempt.

## Where the time and cost went

- **Cold SCIP.** The first prompt writes 4,726 more tokens than No Loom,
  before any Loom response. The first model response costs $0.021350 more
  and later responses cost $0.001727 more. The agent makes one exact symbol
  lookup and uses nine model responses, compared with eight for No Loom.
- **Warm SCIP.** The first response costs $0.001155 less than No Loom, while
  later responses cost $0.011133 more. Both use six model responses. The
  solver processes 147,603 tokens with SCIP and 117,550 without it. Exact
  caller information still leaves a local read of adjacent source and tests.
- **Tree-sitter.** The warm solver uses five responses compared with six
  for No Loom. Later responses cost $0.014643 more. The reduced response
  count and 5.3% reduction in agent time coexist with higher warm cost and
  higher end-to-end time.
- **Tree-sitter with memory.** The warm solver uses eight responses compared
  with six for No Loom. Later responses account for $0.037878 of additional
  cost. The cold trace includes a grep with an unscoped match that widens
  lesson selection, returning 887 lesson characters across three notes.
  The complete server request takes 50 ms, including 37 ms for lessons.
  The retrieval trace captures only the stages before that enrichment.
- **SCIP with memory.** The cold solver uses five model responses and four
  tool calls. It saves $0.027357 on later responses, partly offset by an
  additional $0.021295 on the first response. The warm solver receives the
  fresh callback note from its symbol lookup, then uses
  `include_lessons: false` on both follow-up greps. Neither grep repeats
  the note. It still reads adjacent source and tests and uses seven model
  responses, one more than the warm baseline.

The cache condition here is Anthropic prompt cache state. The Loom index is
prepared before both cache states. Loom's own embedding, answer and continuation
caches are outside the cold/warm manipulation. The pilot gives one observation
per arm per cache state. The equal grades cannot establish a quality lift from
historical memory.

The bound on the cold startup gap and the model response costs identify
two changes to evaluate independently. A smaller upfront MCP tool catalog can
reduce the initial prompt. Returning a requested retained source range can
reduce later work if the next response skips a lookup. Linked notes should
stay tied to matched definitions when their source revision is verified.
The source-window and mixed-scope changes need a fresh end-to-end measurement.
[Source windows](https://github.com/ClickHouse/loom/pull/1272)
[Mixed-scope lessons](https://github.com/ClickHouse/loom/pull/1274)

## Reproduce

Use the [frozen source and controller revision](https://github.com/ClickHouse/loom/tree/3a0d5d0ff8706c2c960765808d4834ef7bab1edd)
and its [environment instructions](https://github.com/ClickHouse/loom/blob/3a0d5d0ff8706c2c960765808d4834ef7bab1edd/examples/software_factory/evals/flask/REPRODUCE.md).
Set the API credential for an isolated workspace in the environment.

```bash
python3 -m examples.software_factory plan --eval flask \
  --pilot-task pallets__flask-4544 --cache-pairs
python3 -m examples.software_factory run --eval flask \
  --pilot-task pallets__flask-4544 --cache-pairs --output ./factory-cli-cache-pairs
```
