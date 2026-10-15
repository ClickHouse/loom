"""The ids of MCP sessions' implicit runs, which the transport makes and
`run.begin` never hands out (`server/transport/mcp_runs.py`)."""

from __future__ import annotations

import hashlib
import json
import re

_SESSION_RUN_ID = re.compile(r"mcp-[0-9a-f]{40}")


def session_run_id(org: str, subject: str, session_id: str) -> str:
    """The implicit run of a token's MCP session: the same for every call and web
    process of the session, and one no other token can make by sending the same
    session id."""
    seed = json.dumps([org, subject, session_id])
    return "mcp-" + hashlib.sha256(seed.encode()).hexdigest()[:40]


def is_session_run_id(run_id: str) -> bool:
    return _SESSION_RUN_ID.fullmatch(run_id) is not None


def ledger_subject(subject: str) -> str:
    """A token subject as `agent_events.subject` records it ("" with no auth)."""
    return "" if subject == "-" else subject
