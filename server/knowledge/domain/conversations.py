"""Conversation values shared by the wire models, the handlers and the store."""

from __future__ import annotations

from typing import Literal

# The `role` column's Enum8 values.
ConversationRole = Literal["user", "assistant", "system", "tool"]

# Characters of a turn its embedding is built from, which the embedding row keeps.
EMBEDDED_EXCERPT_CHARS = 2048  # not a setting: stored rows are matched at this length
