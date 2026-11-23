# Run 80 cost and time by attempt

The five arms ran the same Flask issue. The cold first response read zero
cached tokens. Each warm first response read the corresponding cold write
count and wrote zero. All ten patches passed the hidden tests and blind review.
All four memory attempts received the same fresh callback-order lesson before
the first shell, edit or delegated tool request. The pilots measure one issue
and one attempt per treatment and cache condition.
[Protocol and grades](README.md) · [Verified usage](results.json) · [All model responses and tool calls](SESSIONS.md)

## Where provider cost went

All dollar columns below are USD. First and later model-response costs
reconcile to the solver receipt for each attempt. Loom's provider ledger
attributes zero additional provider cost. The last column is the remaining
reduction needed to reach 20% below No Loom in the same cache condition.

| Start | Arm | First response | Later responses | Total | First delta vs No Loom | Later delta vs No Loom | Above cost target |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Cold | No Loom | 0.09187 | 0.08814 | 0.18001 | — | — | — |
| Cold | Tree-sitter | 0.11351 | 0.07879 | 0.19230 | +0.02164 | −0.00935 | 0.04829 |
| Cold | Tree-sitter + memory | 0.11580 | 0.07759 | 0.19339 | +0.02394 | −0.01055 | 0.04939 |
| Cold | SCIP | 0.11322 | 0.08987 | 0.20309 | +0.02135 | +0.00173 | 0.05908 |
| Cold | SCIP + memory | 0.11316 | 0.06078 | 0.17395 | +0.02130 | −0.02736 | 0.02994 |
| Warm | No Loom | 0.00759 | 0.05835 | 0.06594 | — | — | — |
| Warm | Tree-sitter | 0.00622 | 0.07299 | 0.07921 | −0.00137 | +0.01464 | 0.02646 |
| Warm | Tree-sitter + memory | 0.00594 | 0.09622 | 0.10216 | −0.00166 | +0.03788 | 0.04941 |
| Warm | SCIP | 0.00644 | 0.06948 | 0.07592 | −0.00115 | +0.01113 | 0.02317 |
| Warm | SCIP + memory | 0.00594 | 0.08447 | 0.09040 | −0.00166 | +0.02612 | 0.03765 |

The cost targets are $0.144007 cold and $0.052751 warm. The
cold Loom prompts write 4,715–4,728 more cache tokens than cold No Loom
before the first Loom response. Later cold work costs less in three Loom
arms. In the warm condition, later work alone costs more than the entire
warm No Loom run for all four Loom arms. The warm Tree-sitter + memory
later cost increases by $0.01538 in cache reads, $0.00772 in five-minute
cache writes and $0.01476 in output. Ordinary uncached input contributes
the remaining $0.000016.

## Where time went

Times are seconds except summed Loom server request latency. Request
latencies can overlap. Claude's model API duration includes model, network
and queue time. The trial phase residual is native Harbor elapsed time minus
environment setup, agent setup, agent execution and verification. The factory
residual is the full elapsed timer minus native Harbor elapsed time.

| Start | Arm | Model responses | Model API s | Agent outside API s | Harbor phase residual s | Factory residual s | Loom requests, ms |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Cold | No Loom | 8 | 33.120 | 4.356 | 4.473 | 0.022 | 0 |
| Cold | Tree-sitter | 6 | 21.579 | 4.322 | 5.944 | 1.511 | 63 |
| Cold | Tree-sitter + memory | 6 | 20.527 | 3.944 | 6.206 | 1.702 | 264 |
| Cold | SCIP | 9 | 25.471 | 4.776 | 6.510 | 1.705 | 31 |
| Cold | SCIP + memory | 5 | 16.803 | 3.857 | 6.600 | 1.733 | 54 |
| Warm | No Loom | 6 | 20.537 | 3.612 | 4.273 | 0.030 | 0 |
| Warm | Tree-sitter | 5 | 19.110 | 3.747 | 5.717 | 1.452 | 58 |
| Warm | Tree-sitter + memory | 8 | 26.026 | 3.901 | 5.946 | 1.511 | 114 |
| Warm | SCIP | 6 | 20.117 | 3.902 | 6.300 | 1.599 | 33 |
| Warm | SCIP + memory | 7 | 22.307 | 3.839 | 5.922 | 1.571 | 69 |

Environment and agent setup together take 2.59–3.03 seconds. Verification
takes 8.24–8.49 seconds. Exact time attribution within the phase residual
requires finer instrumentation. A post-trial index snapshot runs in the
factory residual. The end-to-end targets are 42.36 seconds cold and
31.48 seconds warm. The observed warm Loom overhead outside the agent is
18.02–19.17 seconds, leaving 12.31–13.46 seconds for the coding loop at that
target.

## What each agent did

- **No Loom.** Native search and local reads locate source and tests. The
  warm attempt uses five tool calls and six model responses, compared with
  seven calls and eight responses cold. Both patches pass. This sets a
  lower warm baseline for both cost and time.
- **Tree-sitter.** Both starts use two greps. The first returns the validator
  and option declaration bodies and the second returns the test body.
  Neither needs a separate source read for those spans. The responses total
  6,183 characters in both attempts. The cold attempt saves $0.00935 after
  the first response and gives it back at startup. The warm attempt uses
  one fewer model response than No Loom and spends $0.01464 more on later
  responses. Later five-minute writes and output account for most of that
  difference.
- **Tree-sitter + memory.** Both starts receive the fresh callback-order
  lesson with the validator. The cold grep repeats it and returns two
  additional notes after including an unscoped hit. The warm grep repeats
  the same lesson and returns match lines. The agent then reads adjacent
  option source and locates tests locally. The warm attempt takes two
  more model responses than No Loom and spends 5.49 more seconds in the
  model API. The complete Loom requests total 114 ms.
- **SCIP.** Both starts get the validator body and a caller pointer at
  `cli.py:771`. The pointer has no adjacent option declarations. Both
  attempts open the source locally. The warm attempt reads test locations
  in that same local command. It uses six responses, equal to No Loom,
  and processes 30,053 more solver tokens. Output, cache reads and cache
  writes all contribute to its later cost gap.
- **SCIP + memory.** The cold attempt gets the validator, caller pointer
  and fresh note together, then reads nearby source and test locations
  in one local command. Five model responses and four total tool calls
  produce the best cold time. The warm attempt gets the same note, then
  uses two line-only greps and a local read for their surrounding source.
  Both greps disable lessons, so the note does not repeat. It uses seven
  model responses and still spends $0.02612 more than warm No Loom after
  the first response.

Caller source and grep windows address the missing context shown in
these traces. Revision-scoped lessons address the unscoped grep hit.
Startup schema size and these response changes require a paired run to
measure their end-to-end effect.
[Symbol caller source](https://github.com/ClickHouse/loom/pull/1276) ·
[Grep context](https://github.com/ClickHouse/loom/pull/1272) ·
[Grep lesson scope](https://github.com/ClickHouse/loom/pull/1274)
