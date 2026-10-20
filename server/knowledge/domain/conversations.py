"""Conversation values shared by the wire models, the handlers and the store."""

from __future__ import annotations

from typing import Literal

# The `role` column's Enum8 values.
ConversationRole = Literal["user", "assistant", "system", "tool"]

# Characters of a turn its embedding is built from, which the embedding row keeps.
EMBEDDED_EXCERPT_CHARS = 2048  # not a setting: stored rows are matched at this length
# Characters past the excerpt a thread or saved listing shows that it still checks for a
# credential, so a key crossing the cut is caught.
LISTED_MARGIN_CHARS = 1024  # not a setting: security bound on the credential check
# Characters of the last turn the thread list shows.
THREAD_EXCERPT_CHARS = 240  # not a setting: the thread list's wire excerpt length
