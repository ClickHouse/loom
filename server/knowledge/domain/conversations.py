"""Conversation values shared by the wire models, the handlers and the store."""

from __future__ import annotations

import json
from typing import Any, Literal

# The `role` column's Enum8 values.
ConversationRole = Literal["user", "assistant", "system", "tool"]

# Characters of a turn its embedding is built from, which the embedding row keeps.
EMBEDDED_EXCERPT_CHARS = 2048  # not a setting: stored rows are matched at this length
# Characters past the excerpt a thread or saved listing shows that it still checks for a
# credential, so a key crossing the cut is caught.
LISTED_MARGIN_CHARS = 1024  # not a setting: security bound on the credential check
# Characters of the last turn the thread list shows.
THREAD_EXCERPT_CHARS = 240  # not a setting: the thread list's wire excerpt length


def metadata_object(raw: object) -> dict[str, Any]:
    """The metadata JSON object as a dict; `{}` when it is empty or not a JSON object."""
    try:
        meta = json.loads(str(raw or "{}"))
    except ValueError:
        return {}
    return meta if isinstance(meta, dict) else {}


def title_refused(raw_metadata: object) -> bool:
    """Whether the titler marked the thread: its title or its opening turns read as a
    credential, so no later pass asks the model again."""
    return metadata_object(raw_metadata).get("title_refused") is True
