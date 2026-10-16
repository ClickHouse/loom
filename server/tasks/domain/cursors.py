"""Event-cursor reads of the task log."""

from __future__ import annotations

# The watermark an event-cursor read stays behind, on the server's clock. The
# read is sequential, so it sees every row another replica has committed; the
# lag only covers a row stamped but not yet committed.
LAG_SECONDS = 3
