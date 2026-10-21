"""Event-cursor reads of the task log."""

from __future__ import annotations

# The watermark an event-cursor read stays behind, on the server's clock. The
# read is sequential, so it sees every row another replica has committed; the
# lag only covers a row stamped but not yet committed.
LAG_SECONDS = 3

# The patch key of a created event a retry logged for a create cut off before its own.
# Readers keep only a task's first created event: past the dedup window the create's
# own event and the backfill can both be stored.
BACKFILLED = "backfilled"
