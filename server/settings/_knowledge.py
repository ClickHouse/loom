"""Knowledge documents and conversations: chunking, search floor, thread titles."""

from __future__ import annotations

from pydantic import BaseModel, Field


class KnowledgeSettings(BaseModel):
    # ---- conversations ----
    # conversation.search cosine floor; without it the top_k nearest threads
    # return however far. Stricter than memory recall: chat excerpts are noisy.
    conversation_search_min_similarity: float = 0.30

    # LLM-title untitled threads in namespaces with conversation_logging_enabled
    # (the Sessions "Recording" toggle is the off-switch). Skipped with the fake
    # LLM.
    conversation_titler_enabled: bool = True
    # Seconds between titler passes.
    conversation_titler_interval_seconds: int = 1800
    # Only threads with a turn in the last this-many hours are titled.
    conversation_titler_window_hours: int = 168
    # Threads one pass titles at most.
    conversation_titler_max_threads_per_pass: int = 10
    # Turns a thread needs before it is titled.
    conversation_titler_min_turns: int = 4

    # ---- search ----
    # Namespaces an all_mine knowledge.search reads besides the addressed one,
    # at most; the rest come back in `namespaces_skipped`. Each search leg is
    # one statement over all of them (measured 110 ms at 10,000 collections).
    knowledge_search_max_namespaces: int = Field(default=10_000, ge=1)
    # Namespace kinds an all_mine knowledge.search spans (comma list over
    # loom.namespaces.kind): the memory fan-out's, and the caller's collections.
    knowledge_search_fanout_kinds: str = "memory,collection,personal"

    # ---- chunking ----
    # Sliding-window chunking on knowledge.add/upsert, in characters.
    # chunk_size 0 = one chunk per doc (hurts long-form retrieval); stride <
    # chunk_size overlaps. Smaller chunks mean more embed calls; 1500/1230
    # stays well under the text-embedding-3 context.
    knowledge_chunk_size: int = 1500  # characters per chunk; 0 disables
    knowledge_chunk_stride: int = 1230  # stride < chunk_size produces overlap
