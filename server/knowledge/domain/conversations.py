"""Conversation values shared by the wire models, the handlers and the store."""

from __future__ import annotations

from typing import Literal

# The `role` column's Enum8 values.
ConversationRole = Literal["user", "assistant", "system", "tool"]

# Characters of a turn its embedding is built from, which the embedding row keeps.
EMBEDDED_EXCERPT_CHARS = 2048
# Characters of a turn that thread and saved listings check for a credential; past the
# 240-character excerpt, so a key crossing the cut is caught.
LISTED_TEXT_CHARS = 1024  # not a setting: security bound on the credential check
