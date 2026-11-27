# Cost and tool trace by arm

In the CLI option-order replay, SCIP spent $0.0142212 more than No Loom.
Its first model response cost $0.0207650 more. The remaining responses cost
$0.0065438 less. Both attempts solved the issue and passed the blind judgment.
SCIP used one fewer model response and completed agent execution 7.69 seconds
sooner. This is a one-issue, one-attempt-per-arm comparison. [Run results](results.json)

## Five-arm trace

| Arm                  | Model responses | Loom calls / total | First response USD | Later responses USD | Traced Loom server time |
| -------------------- | --------------: | -----------------: | -----------------: | ------------------: | ----------------------: |
| No Loom              |               7 |              0 / 8 |          0.0926880 |           0.0766808 |                       — |
| Tree-sitter          |               6 |              2 / 5 |          0.0273446 |           0.0701530 |                   54 ms |
| Tree-sitter + memory |               5 |              2 / 4 |          0.0272996 |           0.0677550 |                  184 ms |
| SCIP                 |               6 |              1 / 5 |          0.1134530 |           0.0701370 |                   27 ms |
| SCIP + memory        |               6 |              2 / 5 |          0.0059252 |           0.0761266 |                   79 ms |

The prices by response are estimates reconciled to Claude Code's reported
total. Traced Loom time sums the server operation latencies. Client transport,
model time and local commands have separate timings.

- **No Loom.** The first shell call located and read the callback. The next
  read the option declarations. A later edit removed an unsupported issue
  number from the changelog. This is the baseline for model responses, costs
  and verifier results.
- **Tree-sitter.** Exact symbol lookup located the callback. Unscoped grep
  found the option and test references, then the agent read the option
  declaration, test and changelog together. Retained grep windows cover that
  source read. The newest decorator association supplies the owning function
  for the option, so the reference has the right enclosing scope.
- **Tree-sitter + memory.** Both lookups returned the same fresh 282-character
  callback note. The agent then read source, tests and changelog together and
  finished in five model responses. Lesson retrieval accounted for 160 ms
  of the 174 ms exact lookup. This is the slowest individual Loom operation
  across the five attempts and the fastest agent attempt overall. The time
  trace supports measuring lesson work separately from its effect on later
  model responses.
- **SCIP.** The symbol response included an exact caller. The agent went
  straight to a shell call that read a wider source span and looked up tests.
  This path points to better source context around caller sites. The cost
  overage occurred before that caller arrived.
- **SCIP + memory.** The agent received the same fresh note twice and still
  used grep, a combined source and test read, and a second test run on the
  workspace with the source patch stashed. The warm first response accounted
  for almost all of its observed cost advantage over No Loom. Later responses
  were 0.7% cheaper.

The observed duplicate note led to an opt-out for lesson retrieval on a
follow-up grep. The source read led to opt-in retained grep windows. The same
single issue ties on hidden and judged quality across all five arms, so the
trace establishes note delivery and opportunities to reduce work. To measure
quality lift, use an issue whose verifier distinguishes the historical
constraint from a locally plausible patch.
[Follow-up grep lesson control](https://github.com/ClickHouse/loom/pull/1271)
[Retained grep windows](https://github.com/ClickHouse/loom/pull/1272)

## Provider accounting

The attempt totals come from Claude Code’s final reported cost. Token counts
come from Anthropic’s usage in the Claude Code stream. The four token classes
below reconcile to those reported costs using the qualified Opus 5.5 rates.
Each trial’s native trajectory supplies per-response estimates, which sum to
the reported trial cost within $0.00000001. Loom upstream provider cost is zero
in both attempts. Judge and infrastructure costs are outside this comparison.

| Token class              | No Loom tokens | SCIP tokens | No Loom USD |  SCIP USD | SCIP cost change |
| ------------------------ | -------------: | ----------: | ----------: | --------: | ---------------: |
| Uncached input           |             14 |          12 |   0.0000560 | 0.0000480 |       −0.0000080 |
| Cache reads              |        115,939 |     121,260 |   0.0231878 | 0.0242520 |       +0.0010642 |
| Five-minute cache writes |         21,101 |      25,818 |   0.1055050 | 0.1290900 |       +0.0235850 |
| Output                   |          2,031 |       1,510 |   0.0406200 | 0.0302000 |       −0.0104200 |
| Total                    |        139,085 |     148,600 |   0.1693688 | 0.1835900 |       +0.0142212 |

The first prompts in both arms read zero cached tokens and each had two
uncached input tokens. SCIP wrote 22,297 tokens and No Loom wrote 17,624.
That difference added $0.0233650 before Loom returned any code. The first
SCIP response generated 130 fewer tokens, saving $0.0026000. The remaining
SCIP responses wrote just 44 more tokens than No Loom over the rest of the
attempt. The cost overage is concentrated at startup in this trial.

## Tool trace

Claude requested `_validate_key` by path and name. SCIP returned the retained
callback and an exact compiler caller at line 771 of `src/flask/cli.py`. The
next command read the surrounding source and searched the test file. The
returned caller identified where to look. It did not carry the neighboring
certificate option declaration needed to evaluate initialization order.

| SCIP response | Tool selected                     | Tool result characters | Input tokens | Cache writes | Output tokens | Response estimate USD |
| ------------: | --------------------------------- | ---------------------: | -----------: | -----------: | ------------: | --------------------: |
|             1 | `code.symbol` with caller summary |                  1,823 |       22,299 |       22,297 |            98 |             0.1134530 |
|             2 | Source and test lookup            |                  3,351 |       23,215 |          916 |           152 |             0.0120874 |
|             3 | Patch script and test             |                    144 |       24,790 |        1,575 |           571 |             0.0239456 |
|             4 | Changelog lookup                  |                    169 |       25,410 |          620 |            63 |             0.0093256 |
|             5 | Changelog edit and diff summary   |                    133 |       25,556 |          146 |           179 |             0.0093996 |
|             6 | Final answer                      |                      0 |       25,820 |          264 |           447 |             0.0153788 |

No Loom used eight tool calls over seven responses, compared with five tool
calls over six responses for SCIP. SCIP received 5,620 result characters and
No Loom received 4,090. Native API time was 19.96 versus 27.62 seconds. That
timer includes model, transport and queuing time. Tool result characters are
an output-size measure, with separate counters for provider token usage.

| No Loom response | Tools selected                      | Tool result characters | Input tokens | Cache writes | Output tokens | Response estimate USD |
| ---------------: | ----------------------------------- | ---------------------: | -----------: | -----------: | ------------: | --------------------: |
|                1 | Source search and callback read     |                  1,684 |       17,626 |       17,624 |           228 |             0.0926880 |
|                2 | Read nearby option declarations     |                    549 |       18,491 |          865 |           233 |             0.0125178 |
|                3 | Edit and inspect tests              |                  1,057 |       18,940 |          449 |           289 |             0.0117308 |
|                4 | Edit and run tests                  |                    512 |       19,759 |          819 |           445 |             0.0167906 |
|                5 | Edit                                |                     62 |       20,457 |          698 |           193 |             0.0113094 |
|                6 | Clean up changelog and inspect diff |                    226 |       20,678 |          221 |           201 |             0.0092240 |
|                7 | Final answer                        |                      0 |       21,103 |          425 |           442 |             0.0151082 |

## Engineering priorities

1. **Reduce the upfront descriptor cost.** The four upfront coding tools take
   3,927 additional input tokens under the standard profile and 3,158 under
   the compact profile in a controlled Anthropic token-count request. The
   profiles expose the same tool names. Test compact discovery and invocation
   before changing a default. These isolated counts measure descriptor size.
   The native Claude Code prompt can include other setup text and tool metadata.
   At the qualified five-minute write rate, 769 fewer tokens would save
   $0.003845 on the first cold request. The replay's SCIP attempt needed
   $0.048095 less in total to meet the 20% cold-cost target. Reaching that
   target also requires savings beyond these four descriptors.
   [Anthropic token counting](https://platform.claude.com/docs/en/build-with-claude/token-counting)
2. **Return the requested source span.** In the retained option search, six
   exact matches can carry 2,286 characters of nearby indexed source. The
   enclosing definitions carry 4,659 characters. The relevant 704-character
   span covers all of the source lines the next command read with Tree-sitter,
   Tree-sitter with memory, and SCIP with memory.
   Those commands also read tests or changelog content. The test file is
   indexed and Loom can serve its test definition. The changelog is outside
   the configured roots. A new paid trajectory must establish whether the
   shorter source windows save a turn.
   [Grep source windows](https://github.com/ClickHouse/loom/pull/1272)
3. **Measure memory delivery before crediting quality.** Record whether a
   linked fact arrived before editing and whether its code link was fresh at
   the task base. Use the same issue and executable verifier for each arm.
   Use history from before the issue and keep the fixing patch and verifier
   out of the history. The option-order issue reached a five-arm quality tie.
4. **Join metrics by their source.** Claude Code records provider token counts.
   Loom records request latency, retrieval stages and upstream spend. Keep the
   request and tool-call evidence searchable without publishing prompts or
   memory content. Track first-prompt reads and writes, later usage, and full
   attempt cost separately, with missing counters shown as unknown.
   [Loom telemetry](../../../../../../docs/features/observability/telemetry.md)
5. **Separate one-time index work from recurring solver work.** An existing
   index is part of the service state for each coding attempt. Record parser
   and compiler setup, retained source revision, storage and memory seeding as
   separate costs. An amortized comparison needs a stated number of tasks and
   a measured resource price. The report uses provider charges and preserves
   setup evidence, while the infrastructure price remains unknown.

## Cache-controlled comparison

Equal-cold and equal-warm estimates use the same first-input pricing rule
for every arm. They hold later usage fixed. SCIP is 8.4% costlier in the
equal-cold estimate and 9.7% cheaper in the equal-warm estimate. A measured
comparison needs every arm to run a cold start and then a fresh warm attempt
using its own preceding cold run. Check the provider cache counters in each
attempt, keep misses visible, and show the cold and warm results separately.
The run-order and token sources are part of the evidence for a savings claim.
[Replay results and reproduction](README.md)

The controller saves `study/live-attempts.json`, a token record in
`study/diagnostics/attempt-NN-model-usage.json`, and a tool response summary in
`study/diagnostics/attempt-NN.json`. Join model steps to tool responses by tool
call ID. The first step precedes every tool result. The native final Claude Code
usage and total are the source for the attempt counters and headline cost.
The public result hashes the retained artifacts so a reproduction can identify
which source records were used. [Run results](results.json)
