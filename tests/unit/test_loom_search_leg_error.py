"""A loom_search leg that failed says why in its section, code first."""

from __future__ import annotations

import logging

import pytest

from server.core.errors import EmbedTimeoutError
from server.memory.app.loom_search.fanout import _leg_error

_STATEMENT = "Code: 60. DB::Exception: Table loom.memory does not exist. (UNKNOWN_TABLE) SELECT * FROM loom.memory"


def test_a_failed_leg_leads_with_a_loom_errors_code():
    assert _leg_error("memory", EmbedTimeoutError("no answer")) == "EMBED_TIMEOUT: no answer"
    assert _leg_error("memory", EmbedTimeoutError()) == "EMBED_TIMEOUT"


def test_a_store_error_names_its_kind_and_logs_the_text(caplog: pytest.LogCaptureFixture):
    with caplog.at_level(logging.WARNING, logger="server.memory.app.loom_search.fanout"):
        shown = _leg_error("knowledge", RuntimeError(_STATEMENT))
    assert shown == "RuntimeError"
    assert _STATEMENT in caplog.text
    assert "knowledge" in caplog.text
