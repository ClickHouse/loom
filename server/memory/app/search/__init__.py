"""memory.search, memory.search_trace and memory.search_trace_log.

`handle_memory_search` (handler.py) is the retrieval orchestrator; each module
of this package holds one responsibility of the pipeline it runs, and imports
only modules below it. The recursive fallbacks (consistency.py, fallbacks.py)
re-enter through this package's `handle_memory_search`, so a caller and a
re-entry resolve the same name. Every module logs to the package's logger,
`server.memory.app.search`.
"""

from server.memory.app.search.handler import handle_memory_search
from server.memory.app.search.trace import (
    handle_memory_search_trace,
    handle_memory_search_trace_log,
)

__all__ = [
    "handle_memory_search",
    "handle_memory_search_trace",
    "handle_memory_search_trace_log",
]
