"""The text a memory row is embedded under: its value, its key read as words,
and a procedure's situation."""

from __future__ import annotations

import re
from typing import Any

from server.memory.domain.digest_keys import is_digest_key
from server.memory.domain.excerpt import build_excerpt
from server.settings import settings


def procedural_text_source(memory_type: str, value: Any) -> str:
    """For a `procedural` row whose value is the procedure dict, the text an
    embedder and a reader should see: situation first (the match key), then
    name and steps. Empty for anything else, so callers fall back to the
    serialized value. Graph, counters and traces never enter the vector."""
    if memory_type != "procedural" or not isinstance(value, dict):
        return ""
    situation = str(value.get("situation") or "").strip()
    if not situation:
        return ""
    parts = [situation]
    name = str(value.get("name") or "").strip()
    if name:
        parts.append(name.replace("_", " "))
    steps = value.get("steps")
    if isinstance(steps, list) and steps:
        parts.append("; ".join(str(x) for x in steps if str(x).strip()))
    return "\n".join(parts)


def key_axis_text(memory_key: str) -> str:
    """The text the key-axis vector (`memory_key_embedding`) is built from: the humanized key
    ("user_dog_name" -> "user dog name"), so a query shaped like the key matches when the
    value cosine is weak. Every write path and `loom reembed` call this one function so a key
    embeds one way. A key that is only an imported item's digest (`is_digest_key`) has no
    words a query could share: "", so the row gets no key vector and the key axis reads its
    value vector, as for any key without axis text."""
    key = str(memory_key or "")
    if is_digest_key(key):
        return ""
    return key.replace("_", " ").strip().lower()


def _build_embed_text(
    serialized_value: str,
    *,
    memory_key: str = "",
    question_topic: str = "",
) -> str:
    """The text we EMBED — distinct from the display/lexical excerpt.

    Display, BM25 columns, and conflict-check keep the clean
    `build_excerpt`; only the embedding vector sees topic-keying. When
    `memory_embed_answer_topic` is on and `question_topic` is set (extractor
    marks genuine assistant-direct-answer facts), prepend the topic so a
    later low-overlap follow-up that cites the topic (not the answer
    content) cosine-matches. The verbatim value is never touched.
    """
    topic = (question_topic or "").strip()
    base = build_excerpt(
        serialized_value,
        memory_key=memory_key,
        is_answer=bool(topic),
    )
    prefix = ""
    if topic and settings.memory_embed_answer_topic:
        # Skip when the topic's words are already in the value (no dilution).
        base_words = set(re.findall(r"[a-z0-9]+", base.lower()))
        topic_words = re.findall(r"[a-z0-9]+", topic.lower())
        if topic_words and not all(w in base_words for w in topic_words):
            prefix = f"[topic: {topic}] "
    return prefix + base if prefix else base


def embedding_texts(
    memory_type: str, value: Any, serialized_value: str, memory_key: str
) -> tuple[str, str, str]:
    """(excerpt, embed text, key-axis text) of one memory row — the texts memory.set stores
    and embeds, and every path that re-embeds a row must reproduce.

    Procedural rows carry their graph, counters and trace log in the value; embedding that
    JSON would let statistics churn drift the situation vector on every evolve, so the
    procedure's text (situation, name, steps) stands in; the verbatim value column is
    untouched. The embed text is the topic-keyed text (conflict-check canonicalizes the RAW
    serialized value, not this). The key axis is the humanized memory_key, so a query shaped
    like the key matches even when the value cosine is weak; "" when the key has none, an
    imported item's digest key included (the excerpt and the embed text then carry the value
    alone)."""
    text_source = procedural_text_source(memory_type, value) or serialized_value
    return (
        build_excerpt(text_source, memory_key=memory_key),
        _build_embed_text(text_source, memory_key=memory_key),
        key_axis_text(memory_key),
    )
