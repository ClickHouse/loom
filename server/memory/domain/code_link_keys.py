"""The keys of a memory's links to code and of the link worker's queue."""

from __future__ import annotations

# a memory's identity: the columns a link and a queued write name it by
IDENTITY = ("org", "namespace", "agent", "user_id", "session_id", "memory_key")
# a link's sort key
LINK_KEY = ("org", "code_namespace", "path", "qualified_name", "namespace", "memory_id")
# a queued write's position: (queued_at µs, version, *IDENTITY)
QUEUE_PREFIX = ("queued_at", "version")
QueuePosition = tuple[int, int, str, str, str, str, str, str]
