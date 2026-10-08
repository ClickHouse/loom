"""memory.ask — server-side grounded synthesis over recalled memories.

memory.search returns raw memories; context.pack returns formatted sections;
neither synthesizes. memory.ask closes that gap the way code.ask does for the
code plane: retrieve the top-k, then have loom's configured LLM write a
concise, SPECIFIC answer grounded ONLY in those memories, returning the
memory_keys it drew on as citations.

Flow (mirrors code.app.answers.ask):

    retrieve (handle_memory_search, top_k)
        abstains / empty       -> abstained
    allow_synthesis is False   -> retrieval_only (agent synthesizes)
    namespace over cost cap    -> retrieval_only, abstain_reason="budget_capped"
    else                       -> grounded LLM synthesis -> synthesized

Unlike code.ask there is NO answer cache. code.ask memoizes on a per-file
content hash and tombstones an entry when a cited file changes at merge;
memory rows have no such invalidation handle and mutate continuously
(supersession, consolidation, redaction), so a cached synthesized answer would
go stale silently. Every call retrieves fresh — the "cached" source of
code.ask therefore has no memory-plane analogue and is intentionally absent.

The package splits the flow by responsibility: `pipeline` runs the phases,
`pool` builds the evidence pool, `abstain` decides whether to synthesize,
`reader` and `cascade` drive the synthesis calls, `synthesis` makes one,
`rendering` and `prompts` build its prompt, and the leaf modules
(`question_shapes`, `supersession`, `identifiers`, `completion`,
`context_blocks`, `retrieval_legs`, `records`, `result`, `state`) hold the
steps they share.
"""

from server.memory.app.ask.pipeline import ask
from server.memory.app.ask.result import MemoryAskResult

__all__ = ["MemoryAskResult", "ask"]
