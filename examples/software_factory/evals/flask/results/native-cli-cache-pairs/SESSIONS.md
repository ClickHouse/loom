# Run 80 sessions by model response

Each row records one model response from the native Harbor trajectory and
describes its observed tool calls. Parallel calls share a row. The cost uses
the recorded per-response estimate. The estimates sum to the solver receipt
within rounding. Tool descriptions summarize actions and omit command bodies
and private session content. Every patch passed the independent hidden grader
and blind review.

[Cost and time analysis](DIAGNOSTICS.md) · [Verified usage](results.json)

## Cold · No Loom

8 model responses · 0 Loom / 7 total tool calls · $0.1800088 · 37.48 s agent time

| Response | Input tokens | Output tokens | Cost USD | Calls | Observed action |
| ---: | ---: | ---: | ---: | ---: | --- |
| 1 | 17,626 | 187 | 0.091868 | 1 | Search option and validator names and read the source in one shell call |
| 2 | 19,171 | 415 | 0.019558 | 1 | Make the certificate option eager and locate the existing regression test |
| 3 | 20,010 | 334 | 0.014717 | 1 | Add a reversed-option test, run the CLI suite and try the test with both changes stashed |
| 4 | 20,452 | 215 | 0.010520 | 1 | Stash only the source change, confirm the regression test fails and rerun certificate tests after restoring the fix |
| 5 | 20,745 | 85 | 0.007263 | 1 | Read the changelog header |
| 6 | 20,977 | 174 | 0.008797 | 1 | Add a changelog entry and show the diff |
| 7 | 21,907 | 180 | 0.012453 | 1 | Remove the issue reference from the changelog entry |
| 8 | 22,200 | 449 | 0.014834 | 0 | Final response |

## Cold · Tree-sitter

6 model responses · 2 Loom / 5 total tool calls · $0.1923018 · 25.90 s agent time

| Response | Input tokens | Output tokens | Cost USD | Calls | Observed action |
| ---: | ---: | ---: | ---: | ---: | --- |
| 1 | 22,354 | 87 | 0.113508 | 1 | Loom grep returns the validator and option declaration bodies |
| 2 | 24,680 | 252 | 0.021148 | 1 | Loom grep returns the certificate test body |
| 3 | 25,427 | 437 | 0.017419 | 1 | Patch source and tests in one shell call and read the changelog header |
| 4 | 26,013 | 210 | 0.012223 | 1 | Add the changelog entry, run the CLI suite and inspect the diff summary |
| 5 | 26,330 | 171 | 0.010215 | 1 | Remove the issue reference from the changelog entry |
| 6 | 26,689 | 536 | 0.017789 | 0 | Final response |

## Cold · Tree-sitter + memory

6 model responses · 3 Loom / 8 total tool calls · $0.1933944 · 24.47 s agent time

| Response | Input tokens | Output tokens | Cost USD | Calls | Observed action |
| ---: | ---: | ---: | ---: | ---: | --- |
| 1 | 22,341 | 205 | 0.115803 | 2 | Parallel Loom symbol and grep return the validator, fresh note and matching lines. Grep repeats the note and adds two more |
| 2 | 24,823 | 100 | 0.018886 | 1 | Read the option decorator range locally |
| 3 | 25,145 | 364 | 0.013862 | 2 | Edit the certificate option and fetch the certificate test body with Loom grep |
| 4 | 26,118 | 397 | 0.017842 | 2 | Edit the test and locate the changelog section |
| 5 | 26,665 | 208 | 0.012126 | 1 | Add the changelog entry and run the CLI suite |
| 6 | 26,892 | 420 | 0.014876 | 0 | Final response |

## Cold · SCIP

9 model responses · 1 Loom / 8 total tool calls · $0.2030858 · 30.25 s agent time

| Response | Input tokens | Output tokens | Cost USD | Calls | Observed action |
| ---: | ---: | ---: | ---: | ---: | --- |
| 1 | 22,352 | 73 | 0.113218 | 1 | Loom symbol returns the validator body and one caller pointer |
| 2 | 23,152 | 131 | 0.011098 | 1 | Search certificate strings locally. Results include a converter message before the option declaration |
| 3 | 23,538 | 64 | 0.007848 | 1 | Read the option decorator range locally |
| 4 | 23,938 | 266 | 0.012035 | 1 | Edit the source and locate the existing test |
| 5 | 24,635 | 383 | 0.015940 | 1 | Add the reversed-order test and run the CLI suite and focused test |
| 6 | 25,137 | 151 | 0.010465 | 1 | Stash only the source change and confirm the new test fails on the original source |
| 7 | 25,412 | 87 | 0.008150 | 1 | Read the changelog header |
| 8 | 25,646 | 169 | 0.009640 | 1 | Edit the changelog and inspect the source diff |
| 9 | 26,097 | 365 | 0.014692 | 0 | Final response |

## Cold · SCIP + memory

5 model responses · 1 Loom / 4 total tool calls · $0.1739470 · 20.66 s agent time

| Response | Input tokens | Output tokens | Cost USD | Calls | Observed action |
| ---: | ---: | ---: | ---: | ---: | --- |
| 1 | 22,341 | 73 | 0.113163 | 1 | Loom symbol returns the validator, caller pointer and fresh linked note |
| 2 | 23,313 | 153 | 0.012396 | 1 | Read the option declaration and locate test lines in one shell call |
| 3 | 24,367 | 551 | 0.020960 | 1 | Patch source and test, run the CLI suite and inspect the changelog header in one shell call |
| 4 | 25,022 | 179 | 0.011736 | 1 | Add the changelog entry and show the diff summary |
| 5 | 25,286 | 468 | 0.015692 | 0 | Final response |

## Warm · No Loom

6 model responses · 0 Loom / 5 total tool calls · $0.0659390 · 24.15 s agent time

| Response | Input tokens | Output tokens | Cost USD | Calls | Observed action |
| ---: | ---: | ---: | ---: | ---: | --- |
| 1 | 17,626 | 203 | 0.007593 | 1 | Search and read the converter and validator locally |
| 2 | 18,857 | 132 | 0.012328 | 1 | Read the option decorator range locally |
| 3 | 19,218 | 232 | 0.010224 | 1 | Patch the option and locate the test |
| 4 | 19,771 | 276 | 0.012136 | 1 | Edit the test and read the changelog header |
| 5 | 20,194 | 201 | 0.010097 | 1 | Add the changelog entry and run the CLI suite |
| 6 | 20,421 | 419 | 0.013561 | 0 | Final response |

## Warm · Tree-sitter

5 model responses · 2 Loom / 7 total tool calls · $0.0792072 · 22.86 s agent time

| Response | Input tokens | Output tokens | Cost USD | Calls | Observed action |
| ---: | ---: | ---: | ---: | ---: | --- |
| 1 | 22,354 | 87 | 0.006218 | 1 | Loom grep returns the validator and option declaration bodies |
| 2 | 24,681 | 423 | 0.024573 | 2 | Edit the certificate option and fetch the certificate test body with Loom grep |
| 3 | 25,711 | 432 | 0.018734 | 2 | Edit the test, run focused certificate tests and inspect the changelog |
| 4 | 26,396 | 256 | 0.013695 | 2 | Edit the changelog and run the CLI suite |
| 5 | 26,752 | 446 | 0.015987 | 0 | Final response |

## Warm · Tree-sitter + memory

8 model responses · 2 Loom / 9 total tool calls · $0.1021598 · 29.93 s agent time

| Response | Input tokens | Output tokens | Cost USD | Calls | Observed action |
| ---: | ---: | ---: | ---: | ---: | --- |
| 1 | 22,341 | 73 | 0.005936 | 1 | Loom symbol returns the validator and fresh linked note |
| 2 | 23,227 | 211 | 0.013126 | 1 | Loom grep returns matching lines and repeats the note |
| 3 | 24,235 | 91 | 0.011513 | 1 | Read the surrounding option declarations and other eager options locally |
| 4 | 24,587 | 457 | 0.015755 | 2 | Edit the certificate option and locate the test body locally |
| 5 | 25,574 | 398 | 0.017820 | 2 | Edit the test and inspect the changelog |
| 6 | 26,194 | 201 | 0.012242 | 1 | Add a changelog entry and run the CLI suite |
| 7 | 26,421 | 140 | 0.009181 | 1 | Remove the issue reference from the changelog entry |
| 8 | 26,680 | 500 | 0.016587 | 0 | Final response |

## Warm · SCIP

6 model responses · 1 Loom / 5 total tool calls · $0.0759168 · 24.02 s agent time

| Response | Input tokens | Output tokens | Cost USD | Calls | Observed action |
| ---: | ---: | ---: | ---: | ---: | --- |
| 1 | 22,352 | 98 | 0.006438 | 1 | Loom symbol returns the validator and a compact caller summary |
| 2 | 23,269 | 145 | 0.011963 | 1 | Read the option declaration and locate test lines locally |
| 3 | 24,315 | 522 | 0.020331 | 1 | Patch source and tests and run the CLI suite |
| 4 | 25,130 | 86 | 0.010666 | 1 | Read the changelog header |
| 5 | 25,363 | 179 | 0.009779 | 1 | Edit the changelog and inspect the diff summary |
| 6 | 25,627 | 517 | 0.016740 | 0 | Final response |

## Warm · SCIP + memory

7 model responses · 3 Loom / 7 total tool calls · $0.0904042 · 26.15 s agent time

| Response | Input tokens | Output tokens | Cost USD | Calls | Observed action |
| ---: | ---: | ---: | ---: | ---: | --- |
| 1 | 22,341 | 73 | 0.005936 | 1 | Loom symbol returns the validator, caller pointer and fresh linked note |
| 2 | 23,316 | 333 | 0.016011 | 2 | Parallel Loom greps locate certificate source and key test lines with lessons disabled |
| 3 | 24,851 | 147 | 0.015286 | 1 | Read both surrounding ranges locally |
| 4 | 25,366 | 552 | 0.018593 | 1 | Patch source and tests and run the CLI suite |
| 5 | 26,001 | 91 | 0.010076 | 1 | Read the changelog header |
| 6 | 26,239 | 174 | 0.009878 | 1 | Edit the changelog and inspect its diff |
| 7 | 26,601 | 378 | 0.014625 | 0 | Final response |
