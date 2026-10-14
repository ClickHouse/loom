"""Counting the work a call does, for tests that bound how it grows: lines and calls
executed, not seconds, so a loaded machine never decides."""

from __future__ import annotations

import sys
from collections.abc import Callable
from typing import Any


def steps(run: Callable[[], Any]) -> int:
    """The lines and calls `run` executes; `run` must return a truthy result."""
    mon = sys.monitoring
    tool, events = 3, mon.events.LINE | mon.events.CALL  # a free id: 5 is the optimizer's
    count = 0

    def step(*_: Any) -> None:
        nonlocal count
        count += 1

    mon.use_tool_id(tool, "step_count")
    try:
        mon.register_callback(tool, mon.events.LINE, step)
        mon.register_callback(tool, mon.events.CALL, step)
        mon.set_events(tool, events)
        assert run()
    finally:
        mon.set_events(tool, 0)
        mon.free_tool_id(tool)
    return count
