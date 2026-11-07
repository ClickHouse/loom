"""An ingestion's states: the first two are open (a worker may take the job), the
rest are where it ended."""

from __future__ import annotations

OPEN_STATES = ("pending", "processing")
DONE_STATES = ("ready", "partial", "failed", "cancelled")
