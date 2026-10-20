"""Conversation values shared by the wire models, the handlers and the store."""

from __future__ import annotations

from typing import Literal

# The `role` column's Enum8 values.
ConversationRole = Literal["user", "assistant", "system", "tool"]

# Characters of a turn its embedding is built from, which the embedding row keeps.
EMBEDDED_EXCERPT_CHARS = 2048
# Characters of a turn a namespace-wide listing (`conversation.threads`,
# `conversation.list_saved_messages`) reads and checks for a credential before it cuts
# the excerpt it shows: past its 240-character cut, so a key that crosses it is caught,
# without reading a long turn whole.
LISTED_TEXT_CHARS = 1024
