"""code.search composition: shape-routed, hybrid, calibrated, budgeted.

Pipeline:

  1. Deterministic shape classifier — a symbol-shaped query (CamelCase /
     snake_case identifier, `Foo::bar`, `file.cpp:123`) runs the exact
     symbol route and returns WITHOUT computing an embedding. Compiler
     facts beat similarity wherever the question is symbol-shaped.
  2. Hybrid: BM25-ish lexical leg over identifier lexemes + brute-force
     cosine leg over contextual embeddings, fused app-side with RRF
     (k=60). A cheap fuzzy-symbol leg votes in route agreement only.
  3. Calibrated abstention via the shared retrieval_calibration module —
     "no strong match, use grep" beats a plausible wrong snippet.
  4. Personalization strictly as a tie-break (<5% relative score gap),
     never crossing risk bands, always logged in score_explanation.
  5. Token budgeting: whole units only; overflow units return elided
     (signature + pointer).

Modules, each importing only from those listed before it:

- `shape`: the query-shape classifier (symbol, issue, hybrid).
- `state`: `CodeSearchResult`, the request state between phases, pool sizes.
- `priors`: the post-fusion rank priors (fusion signals, kind prior,
  outcome boost, personal tie-break).
- `window`: history anchors, fan-in order, seen-set, diversity, saturation.
- `symbol`: route 1, the exact symbol route, and the fuzzy-symbol vote.
- `routes`: routes 0 and 1, run before any leg.
- `lexical`, `vector`, `history`: the content legs and the history leg.
- `cards`, `hyde`: the card and concept legs, the hypothetical-answer leg.
- `voting`: which voting legs a question gets and what they decide.
- `gather`: route 2's legs, run beside each other under the budget.
- `fusion`: RRF or comparable-score fusion, and leg provenance.
- `calibrate`: calibration and abstention on the raw fusion.
- `rerank`: the optional rerank pass.
- `rank`: fused rows to ranked units through every prior.
- `track`: the track_path diagnostic.
- `serve`: the page, the history section, the score explanation.
- `pipeline`: `search`, which runs the rest.

This module exports what the handler and service layers call; everything
else is imported from the submodule that defines it.
"""

from __future__ import annotations

from server.code.app.search.pipeline import search
from server.code.app.search.shape import classify_query_shape
from server.code.app.search.state import _POOL_KEEP, CodeSearchResult

__all__ = [
    "_POOL_KEEP",
    "CodeSearchResult",
    "classify_query_shape",
    "search",
]
