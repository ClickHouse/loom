"""What memory.ask returns: the result record, the citations parsed from an
answer, the grounding label and the synthesis pool's composition."""

from __future__ import annotations

import re
from dataclasses import dataclass
from dataclasses import field as dc_field
from typing import Any, Literal

from server.memory.app.ask.prompts import _INFERENCE_MARK, _WORLD_KNOWLEDGE_MARK

AnswerGrounding = Literal["", "memory", "mixed"]


def answer_grounding(answer: str, *, allow_world: bool) -> AnswerGrounding:
    """'' for no answer; 'mixed' when the synthesizer marked a bridged or
    inferred sentence under allow_world; otherwise 'memory'."""
    if not (answer or "").strip():
        return ""
    low = answer.lower()
    if allow_world and (_INFERENCE_MARK in low or _WORLD_KNOWLEDGE_MARK in low):
        return "mixed"
    return "memory"


@dataclass
class MemoryAskResult:
    answer: str = ""
    # synthesized | retrieval_only | abstained | record
    source: str = "synthesized"
    abstain: bool = False
    abstain_reason: str = ""
    # The memory_keys the answer drew on (its citations).
    memory_keys: list[str] = dc_field(default_factory=list)
    # Knowledge passages shown to the synthesizer.
    knowledge_hits: int = 0
    # The raw retrieved memories (MemorySearchOutput.results) so callers can
    # still inspect the sources behind the answer.
    results: list[Any] = dc_field(default_factory=list)
    embedding_model: str = ""
    # Synthesis routing (see memory.domain.synth_policy). Empty unless synthesized.
    synth_model: str = ""
    route_tier: str = ""
    route_reason: str = ""
    escalated: bool = False
    # Dated raw turns the second synthesis read.
    when_evidence_turns: int = 0
    # Facet completion: the key facets pulled and rows added.
    facets: list[str] = dc_field(default_factory=list)
    facet_memories_added: int = 0
    # Answer-vs-citation audit (memory.app.answer_consistency).
    # "" when the check did not run.
    consistency: str = ""
    consistency_score: float = 0.0
    consistency_note: str = ""
    # The cited memory the answer contradicted, by key, when the verdict was
    # CONTRADICTED and the judge named one (the handler turns
    # it into a `contradicted` usage event on that memory). Internal; the wire
    # carries the same key inside consistency_note.
    contradicted_key: str = ""
    # Under consistency_mode="deferred" the judge has not run
    # yet; this awaitable runs it (and the precedent store that waits on the
    # verdict) after the response. None when the check ran inline or is off.
    deferred_consistency: Any = None
    # Identifier chase: hops walked from the question's
    # identifiers through the identifiers the injected memories name, and
    # how many memories those hops added to the window.
    identifier_chase_hops: int = 0
    identifier_chase_added: int = 0
    # Similarity knee: "" when the cut was off or skipped;
    # otherwise knee | floor | kept_all, with the counts.
    gate_mode: str = ""
    gate_kept: int = 0
    gate_dropped: int = 0
    # The question's premise against pinned / verified memories in the window:
    # the trusted key it contradicts, and a one-line note.
    premise_conflict: str = ""
    premise_note: str = ""
    # The synthesizer's own premise verdict for the answer
    # returned — "supported", "unsupported", or "" when the line was off or
    # did not parse.
    premise_verdict: str = ""
    # "fired" when the synthesis call carried the
    # computed attribution note under the question, "" otherwise.
    attribution_note: str = ""
    # Reading depth: "excerpt" or "raw"; empty unless synthesized.
    depth: str = ""
    deep_fired: bool = False
    decomposed: list[str] = dc_field(default_factory=list)
    # Superseded rows dropped from the synthesis pool, and the
    # chunk/summary sentences that restated them masked.
    superseded_dropped: int = 0
    stale_sentences_masked: int = 0
    # Bridge entities the second hop followed (a chain question
    # "the X of the Y of Z" re-retrieves around the entity the first hop
    # resolved). Empty when the question is not a chain or nothing bridged.
    entity_followed: list[str] = dc_field(default_factory=list)
    # What the fail-open legs swallowed, and how wide the pool
    # actually was. `decompose_failed` counts sub-question retrievals that
    # raised (0 = every part was retrieved); `pool_k` is the widened top_k the
    # retrieval ran with (0 = the caller's top_k, nothing widened).
    decompose_failed: int = 0
    pool_k: int = 0
    # "memory" = every sentence from the memories; "mixed" = the answer
    # bridged with general knowledge under world_knowledge="allow" and marked
    # the sentences that did; "" = no synthesis ran.
    grounding: AnswerGrounding = ""
    # Raw turns of batches still waiting for extraction that were read into
    # the window; 0 when nothing was pending or matched.
    pending_evidence: int = 0
    deep_reason: str = ""
    deep_selector: str = ""
    deep_keys: list[str] = dc_field(default_factory=list)
    deep_chars: int = 0
    # "single" / "empty_block" / "deep_failed" / "" (see the
    # wire model).
    deep_first: str = ""
    # How many synthesis-family calls ran.
    synth_calls: int = 0
    # Code-plane grounding rows in the synthesis pool (source
    # "code_index"); 0 when the code plane is unbound, disabled or found nothing.
    code_hits: int = 0
    # What the synthesizer's pool was made of — fact rows and
    # verbatim chunk rows (code rows are `code_hits`). The wire's `results`
    # and `memory_keys` list facts only, so without these two counts a
    # window that lost its chunks was invisible from the ledger.
    pool_facts: int = 0
    pool_chunks: int = 0
    # Estimated tokens per block of the synthesis prompt
    # (system, notes, window, chunks, code, chase, knowledge, raw_turns,
    # timeline, recent, scratchpad, elision, graph, question, other), summed
    # over the ask's synthesis-family calls; chars/4, the listing budget's
    # estimate. Empty when nothing was synthesized.
    prompt_blocks: dict[str, int] = dc_field(default_factory=dict)
    # Computed timeline: dated memories the synthesizer was
    # given with their deltas to the question date; 0 when the block did not run.
    timeline_rows: int = 0
    # Synthesis-window token budget (memory.domain.ask_budget): the estimated input
    # tokens of the memories the synthesizer saw, and how many ranked memories
    # the caller's `token_budget` cut from the window (0 = no budget or all fit).
    context_tokens_est: int = 0
    context_trimmed: int = 0
    # Session-summary hits (source="summary", the always-on RRF leg) that
    # survived into the synthesis window. Zero on a workload means the leg's
    # 22k LLM calls a day buy nothing there.
    summary_hits: int = 0
    # The ANSWER count was rewritten from the model's own ITEMS verdicts:
    # the list said N, the model said something else.
    count_recomputed: bool = False


def _by_keys(memories: list[Any], keys: list[str]) -> list[Any]:
    """The grounding memories whose key is in `keys`, in grounding order."""
    want = set(keys)
    return [m for m in memories if str(getattr(m, "memory_key", "") or "") in want]


def _is_chunk_hit(r: Any) -> bool:
    return str(getattr(r, "memory_key", "") or "").startswith("chunk:")


def pool_composition(memories: list[Any]) -> tuple[int, int]:
    """(fact rows, verbatim chunk rows) in a synthesis pool.
    Code-plane rows (source "code_index") are neither: they are `code_hits`."""
    facts = chunks = 0
    for m in memories or []:
        if _is_chunk_hit(m):
            chunks += 1
        elif (getattr(m, "source", "") or "") != "code_index":
            facts += 1
    return facts, chunks


def _keys_of(memories: list[Any]) -> list[str]:
    """Ordered, de-duplicated memory_keys of the given memories."""
    seen: set[str] = set()
    out: list[str] = []
    for m in memories:
        k = getattr(m, "memory_key", "") or ""
        if k and k not in seen:
            seen.add(k)
            out.append(k)
    return out


_CITE_MARKER_RE = re.compile(r"\[([^\[\]]+)\]")
# The other shapes the cheap-tier models write for a key shown as [key]:
# "(#key)", "(#key, #key2)" and a bare "#key" token. Measured on 1,986
# memory.ask answers from gpt-4.1-mini: brackets 570, parentheses
# 659, bare "#key" 661, none 96 — unparsed, two thirds of cited answers read
# as uncited, which escalates the cheap tier, blocks the precedent store and
# fires the deep pass on every call under depth=auto. Every candidate is
# window-filtered below, so a token that is not a window key cites nothing.
_CITE_HASH_RE = re.compile(r"#\s*`?([A-Za-z][A-Za-z0-9_:./\-]{2,})`?")
_CITE_PAREN_RE = re.compile(r"\(([^()]{3,400})\)")


def _keys_cited(answer: str, memories: list[Any]) -> list[str]:
    """The grounding memories the answer cites — as "[key]", "(#key, #key2)" or
    a bare "#key" — in grounding order. Empty when nothing matches (caller
    falls back to citing every grounding memory rather than nothing)."""
    # Normalise the marker text: gpt-4o-mini-class models write
    # "[#user_task_deadline]" or "[`key`]" for a key shown as "[key]" — most
    # cheap answers carry a "#" prefix, which would read as "cited nothing" and
    # escalate every one of them.
    marked = {
        m.group(1).strip().strip("`").lstrip("#").strip() for m in _CITE_MARKER_RE.finditer(answer)
    }
    # "(#key, #key2)" and bare "#key": each comma- or space-separated token
    # inside a parenthesis, and each #-prefixed token anywhere. Only tokens
    # that ARE a window key survive the filter below, so prose in
    # parentheses cites nothing.
    for m in _CITE_PAREN_RE.finditer(answer):
        for tok in re.split(r"[,\s;]+", m.group(1)):
            marked.add(tok.strip().strip("`").lstrip("#").rstrip(".").strip())
    for m in _CITE_HASH_RE.finditer(answer):
        marked.add(m.group(1).rstrip(".").strip())
    marked.discard("")
    if not marked:
        return []
    out: list[str] = []
    for m in memories:
        k = getattr(m, "memory_key", "") or ""
        if k and k in marked and k not in out:
            out.append(k)
    return out
