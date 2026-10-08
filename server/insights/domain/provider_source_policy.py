"""Logical identities that survive removal of the old embedding model label."""

from dataclasses import dataclass


@dataclass(frozen=True)
class SourceSelection:
    keys: tuple[str, ...]
    revision: str


# These tables include the model in their physical sorting key. Rebuilding all
# old labels into one label must choose one full row per model-independent key.
MODEL_KEYED_SOURCES = {
    "code_answers": SourceSelection(("org", "namespace", "question_id"), "version"),
    "code_chunks": SourceSelection(("org", "namespace", "path", "chunk_id"), "version"),
    "code_chunks_prefix": SourceSelection(("org", "namespace", "path", "chunk_id"), "version"),
    "code_summaries": SourceSelection(("org", "namespace", "scope_type", "scope_path"), "version"),
    "conversation_embeddings": SourceSelection(("org", "namespace", "thread_id", "msg_id"), "ts"),
    "entity_summaries": SourceSelection(
        ("org", "namespace", "agent", "user_id", "entity"), "version"
    ),
    "memory_embeddings": SourceSelection(
        ("org", "namespace", "agent", "user_id", "session_id", "scope", "memory_key"),
        "version",
    ),
    "session_summaries": SourceSelection(
        ("org", "namespace", "agent", "user_id", "session_id"), "version"
    ),
}

SOURCE_POLICY_VERSION = 1
