"""A loom_search leg that failed says why in its section, code first."""

from __future__ import annotations

from server.core.errors import EmbedTimeoutError
from server.memory.app.loom_search.fanout import _leg_error


def test_a_failed_leg_leads_with_a_loom_errors_code():
    assert _leg_error(EmbedTimeoutError("no answer")) == "EMBED_TIMEOUT: no answer"
    assert _leg_error(EmbedTimeoutError()) == "EMBED_TIMEOUT"
    assert _leg_error(RuntimeError("boom")) == "boom"
