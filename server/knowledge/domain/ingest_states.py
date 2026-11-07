"""The states in which an ingestion is open: a worker may take the job."""

from __future__ import annotations

OPEN_STATES = ("pending", "processing")
