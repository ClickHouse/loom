"""The budgets tables of `server/settings/catalog.py`, as a call site reads them: the
catalog's value with the deployment's override over it."""

from __future__ import annotations

import logging
import threading

from server.settings.catalog import LLM_MAX_TOKENS, parse_llm_max_tokens

log = logging.getLogger(__name__)

_lock = threading.Lock()
_parsed: tuple[str, dict[str, int]] = ("", {})


def _overrides(raw: str) -> dict[str, int]:
    """`llm_max_tokens_overrides` parsed, re-parsed only when the raw string changes.
    The field refuses a bad map; one stored by a build that had a call this one lacks is
    logged once and treated as empty, so it cannot take the model calls down."""
    global _parsed
    with _lock:
        if raw == _parsed[0]:
            return _parsed[1]
        try:
            out = parse_llm_max_tokens(raw)
        except ValueError as exc:
            log.warning("LOOM_LLM_MAX_TOKENS_OVERRIDES ignored: %s", exc)
            out = {}
        _parsed = (raw, out)
        return out


def llm_max_tokens(call: str) -> int:
    """The most tokens the LLM call `call` may write: its `LLM_MAX_TOKENS` entry, or the
    deployment's `llm_max_tokens_overrides` entry when it has one."""
    from server.settings import settings

    return _overrides(str(settings.llm_max_tokens_overrides)).get(call, LLM_MAX_TOKENS[call])
