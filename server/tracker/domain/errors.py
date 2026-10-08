"""The tracker's one failure value: a read that could not be served."""

from __future__ import annotations


class TrackerUnavailable(RuntimeError):
    """The read could not be served: unconfigured, breaker open, timed out,
    kill switch off, or a restricted namespace without its restricted row."""
