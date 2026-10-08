"""Conversation values shared by the wire models and the store."""

from __future__ import annotations

from typing import Literal

# The `role` column's Enum8 values.
ConversationRole = Literal["user", "assistant", "system", "tool"]
