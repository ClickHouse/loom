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
    The field refuses a bad map at startup, in the settings file and at `settings.set`,
    and a stored override this build cannot use is skipped when it is adopted."""
    global _parsed
    with _lock:
        if raw == _parsed[0]:
            return _parsed[1]
        out = parse_llm_max_tokens(raw)
        _parsed = (raw, out)
        return out


def llm_max_tokens(call: str) -> int:
    """The most tokens the LLM call `call` may write: its `LLM_MAX_TOKENS` entry, or the
    deployment's `llm_max_tokens_overrides` entry when it has one."""
    from server.settings import settings

    return _overrides(str(settings.llm_max_tokens_overrides)).get(call, LLM_MAX_TOKENS[call])
