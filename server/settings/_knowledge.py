"""Knowledge documents and conversations: chunking, search floor, thread titles."""

from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, Field

from server.settings._live import LIVE


class KnowledgeSettings(BaseModel):
    # ---- conversations ----
    # conversation.search cosine floor; without it the top_k nearest threads
    # return however far. Stricter than memory recall: chat excerpts are noisy.
    conversation_search_min_similarity: Annotated[float, LIVE] = 0.30
    # Characters of a turn a conversation.search hit shows; the embedding row keeps
    # the whole embedded text, so this changes no stored row.
    conversation_search_excerpt_chars: Annotated[int, LIVE] = Field(default=240, ge=1)

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

    # ---- chunking ----
    # Sliding-window chunking on knowledge.add/upsert, in characters.
    # chunk_size 0 = one chunk per doc (hurts long-form retrieval). Smaller
    # chunks mean more embed calls; 1500/1230 stays well under the
    # text-embedding-3 context.
    knowledge_chunk_size: Annotated[int, LIVE] = 1500
    # Characters between chunk starts: below chunk_size the chunks overlap; above it
    # the step is chunk_size, so no text falls between chunks.
    knowledge_chunk_stride: Annotated[int, LIVE] = 1230
