"""Tracker handlers and orchestration: what each op and worker pass does, with I/O.

Importing the package installs the gate the live source's reads pass through
(`source.py`).
"""

from server.tracker.app import source as _source  # noqa: F401 — installs the source gate
