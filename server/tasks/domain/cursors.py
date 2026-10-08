"""Event-cursor reads of the task log."""

from __future__ import annotations

# The watermark an event-cursor read stays behind: a row still landing on
# another replica is not skipped for ever.
LAG_SECONDS = 3
