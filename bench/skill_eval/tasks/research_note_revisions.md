---
task: research_note_revisions
namespace: skill-eval-research
agent: skill-eval
traps: new-key-per-revision, hand-rolled-index, hash-keys, base64
---

# Research note: "Should the sidecar retry at all?" — three revisions

You are the research agent. Write a short research note and revise it twice
as new evidence arrives, keeping every version's conclusions in Loom, org
from your token, namespace **`skill-eval-research`**, so that a reader next
month gets the **current** conclusion by default, can see how it changed,
and never lands on a superseded one without knowing it.

## Revision 1 (09:05Z) — from the incident alone

Evidence: during case 4711 the dns-sidecar's five retries with doubling
backoff turned a 2-of-3 resolver loss into a 4.5k→8.9k qps storm on the
surviving coredns replica (08:13–08:19Z), which then CPU-throttled at 94%.
Checkout's own HTTP client also retries (3 attempts), so a single checkout
request could produce up to 15 DNS queries.

Conclusion 1: cap sidecar retries at 2 and make checkout stop retrying on
resolver errors; expected effect: peak query rate on a lone replica falls
from ~9k to ~3k qps.

## Revision 2 (09:40Z) — the platform team's numbers

New evidence: the platform team measured that with `MaxAttempts=2` the
sidecar's 08:13–08:19Z traffic would have been 3.4k qps on the surviving
replica — still 70% over its 2k budget — because the checkout client's
three attempts dominate once the sidecar stops retrying. Also, 61% of the
retried queries were for the same two names (`payments-db`, `redis-cache`).

Conclusion 2 (replaces 1): the retry cap alone is not enough; the sidecar
should additionally serve stale answers for up to 30 s (`serve-stale`) for
names it has resolved before, which would have answered 61% of the retried
queries from cache. Keep the checkout client at 3 attempts.

## Revision 3 (10:20Z) — the coredns maintainer's reply

New evidence: coredns already supports `serve_stale` in the `cache` plugin;
enabling it on the _server_ side is a one-line change and covers every
client, not only the sidecar. With `cache { serve_stale 30s }` the
maintainer estimates the 08:13Z loss would have produced no SERVFAILs at all
for names cached in the previous 30 s — which was 94% of checkout's names.

Conclusion 3 (replaces 2): enable `serve_stale 30s` on coredns, cap sidecar
retries at 2, leave the checkout client alone. Open question: the 6% of names
not cached in the previous 30 s — are any of them on the checkout path?

## What to record

- The note itself (three revisions; the current one is what a reader should
  get; the history must remain visible).
- Each conclusion as a finding a natural-language question can hit ("should
  the sidecar retry", "what does serve_stale buy us").
- The open question, marked as open.
- The evidence figures you relied on (the 8.9k qps peak, the 61%, the 94%),
  each with where it came from.

Some of the evidence lines above contain characters that are awkward to
quote (`→`, `{ }`, `%`); record them as they are — do not encode them.

## Before you finish

Say which key holds the current conclusion. Before you finish, the namespace
will be audited: the grade is the namespace you leave behind, not the note.
