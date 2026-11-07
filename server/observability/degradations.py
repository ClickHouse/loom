"""Per-request record of provider fallbacks — what this response did NOT do.

Loom's computed-fact providers fail open by design: a classify endpoint that
errors keeps the caller's memory_type, a reranker that errors keeps the input
order, a risk annotator that cannot run returns un-annotated rows. Each logs
a warning the caller never sees. To an autonomous agent the degraded answer
is indistinguishable from the normal one.

This module makes the fallback visible on the response. A provider that
falls back calls `note(provider, effect, reason)`; the handler reads
`current()` into the output's `degraded` list. The transport binds a fresh
list per op next to the cost accumulator, so notes never leak across
requests. Bounded, never raises, and a note also feeds `provider_health`
(unless it passes `health=False`: a fallback that is no provider's) so the
/v1/health flag agrees with the responses.

    degraded: ["classify: caller_type_kept (HTTPStatusError: 502 Bad Gateway)",
               "rerank: input_order_kept (ReadTimeout)"]
"""

from __future__ import annotations

import contextvars
import logging

log = logging.getLogger(__name__)

_MAX_NOTES = 8
_MAX_REASON = 160

_notes: contextvars.ContextVar[list[str] | None] = contextvars.ContextVar(
    "loom_degradations", default=None
)


def begin() -> None:
    """Bind a fresh, empty note list for this request."""
    _notes.set([])


def note(provider: str, effect: str, reason: object = "", *, health: bool = True) -> None:
    """Record that `provider` fell back to `effect` because of `reason`.

    `effect` is the short bounded label of what the response did instead
    (`caller_type_kept`, `input_order_kept`, `unannotated`). `health=False` keeps
    a fallback that is no provider's out of `provider_health`. Never raises.
    """
    try:
        text = str(reason) if reason is not None else ""
        if isinstance(reason, BaseException):
            text = f"{type(reason).__name__}: {reason}" if str(reason) else type(reason).__name__
        text = text.replace("\n", " ").strip()[:_MAX_REASON]
        entry = f"{provider}: {effect}" + (f" ({text})" if text else "")
        lst = _notes.get()
        if lst is None:
            # No transport bound a list (a worker, a test): still count it
            # for health, and start a list so a later reader sees it.
            lst = []
            _notes.set(lst)
        if len(lst) < _MAX_NOTES:
            lst.append(entry)
        if not health:
            return
        try:
            from server.observability import provider_health

            provider_health.record_failure(provider, text or effect)
        except Exception:  # noqa: BLE001 — a health counter never fails a note
            pass
    except Exception:  # fail open: a note never fails the request it describes
        log.debug("degradations.note failed", exc_info=True)


def current() -> list[str]:
    """The notes recorded so far in this request (a copy)."""
    lst = _notes.get()
    return list(lst) if lst else []


__all__ = ["begin", "current", "note"]
