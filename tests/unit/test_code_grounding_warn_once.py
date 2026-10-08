"""A fail-open code-plane read warns once per process, then goes quiet."""

from __future__ import annotations

import logging

from server.memory.app import code_grounding as mcg


def test_warn_once_then_debug(caplog) -> None:
    mcg._WARNED.discard("pr-fact grounding failed")
    with caplog.at_level(logging.DEBUG, logger=mcg.logger.name):
        mcg._warn_once("pr-fact grounding failed: %s", RuntimeError("code 47"))
        mcg._warn_once("pr-fact grounding failed: %s", RuntimeError("code 47"))
    levels = [r.levelno for r in caplog.records if "pr-fact grounding failed" in r.getMessage()]
    assert levels[:1] == [logging.WARNING]
    assert levels[1:] == [logging.DEBUG]
