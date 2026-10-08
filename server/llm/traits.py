"""What Loom has learned about each model from its provider's own answers.

Nothing here is keyed on model names, so a model released tomorrow needs no code
change. A client builds the request it would like to send; the provider's
answers then teach this process, once per model:

* **Refused fields.** A 400 that names a field the request carried (OpenAI's
  `error.param`, or the field's name in the error message) means the model does
  not take it: the call is re-sent without it, and later calls leave it out.
  That covers `temperature` on models that only sample at their default,
  `reasoning_effort` on models without reasoning, and an output-cap field the
  endpoint calls by its other name.
* **Lowest effort.** A 400 that refuses a reasoning effort and lists the
  supported values sets the model's effort to the lowest of them.
* **Hidden reasoning.** Reasoning and thinking tokens come out of the same
  output cap as the reply, so a cap sized for the reply can be spent before a
  word of it. Usage reports how many the model spent; the model's reserve is
  twice the most it has spent (at most `llm_hidden_reasoning_reserve_max`) and
  is added to every caller's cap. A reply that hit the cap with no visible text
  is re-sent once with the ceiling as its reserve; its own count then sets the
  model's reserve.
* **Starved by an error.** Some endpoints answer a cap spent on reasoning with a
  400 ("could not finish the message because max_tokens …") instead of a
  `length` stop; it is handled like a starved reply: re-sent with room.
* **Output limit.** A 400 that says the cap is over the model's maximum names
  the maximum; caps are clamped to it from then on.

Learned traits live for the process; a restart relearns them in a call or two.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

# Reasoning efforts from least to most; the lowest a model accepts is used
# unless a deployment pins one.
EFFORT_ORDER = ("none", "minimal", "low", "medium", "high", "xhigh")
# Re-sends one call may spend learning what its model takes.
MAX_NEGOTIATION = 4
# The least reasoning a starved reply is taken to have spent: a tiny cap
# (a 3-token judge) would otherwise grow by a few tokens per try.
STARVED_FLOOR = 1024

_EFFORT_REFUSED_RE = re.compile(r"reasoning_effort.{0,40}does not support '([a-z]+)'")
_SUPPORTED_RE = re.compile(r"Supported values are:([^.]*)")
# "max_tokens: 20000 > 8192, which is the maximum allowed ..." (Anthropic) and
# "... supports at most 16384 completion tokens" (OpenAI).
_OUTPUT_LIMIT_RE = re.compile(
    r"(?:>\s*(\d+),\s*which is the maximum|supports at most (\d+) (?:completion|output) tokens)"
)


@dataclass
class Traits:
    refused: set[str] = field(default_factory=set)
    effort: str = ""
    reserve: int = 0
    # The most output tokens the model takes in one call (0 = not yet refused).
    max_output: int = 0


_TRAITS: dict[str, Traits] = {}


def traits(model: str) -> Traits:
    return _TRAITS.setdefault(model, Traits())


def reset_for_tests() -> None:
    _TRAITS.clear()


def _reserve_max() -> int:
    from server.settings import settings

    return int(settings.llm_hidden_reasoning_reserve_max)


def output_cap(model: str, cap: int) -> int:
    """The caller's cap plus the room this model is known to spend on reasoning,
    within the model's output limit once a refusal has named it."""
    t = traits(model)
    want = cap + t.reserve
    return min(want, t.max_output) if t.max_output else want


def note_hidden(model: str, hidden: int) -> None:
    """Record the reasoning tokens one reply spent."""
    if hidden > 0:
        t = traits(model)
        t.reserve = min(_reserve_max(), max(t.reserve, 2 * hidden))


def starved_cap(model: str, cap: int, hidden: int) -> int:
    """The cap to re-send a reply that spent the whole cap on reasoning: the
    caller's cap plus the full reserve ceiling. A starved reply says only that
    reasoning needs more than it got; the ceiling bounds the re-send (billing
    is for tokens used), and the re-sent reply's own count sets the reserve."""
    note_hidden(model, hidden)
    t = traits(model)
    want = cap + max(_reserve_max(), t.reserve)
    return min(want, t.max_output) if t.max_output else want


def _error_text(body: Any) -> tuple[str, str]:
    """(message, param) of a provider error body, either shape."""
    err = body.get("error") if isinstance(body, dict) else None
    if isinstance(err, dict):
        return str(err.get("message") or ""), str(err.get("param") or "")
    return (str(body) if body else ""), ""


def refused_field(body: Any, payload: dict[str, Any], optional: tuple[str, ...]) -> str | None:
    """The optional request field a 400 body says the model does not take."""
    message, param = _error_text(body)
    if param in optional and param in payload:
        return param
    for name in optional:
        if name in payload and re.search(rf"[`'\"]?\b{re.escape(name)}\b[`'\"]?", message):
            return name
    return None


def learn_output_limit(model: str, body: Any) -> bool:
    """Whether a 400 named the model's output limit (then remembered; the call is
    re-sent within it)."""
    message, _ = _error_text(body)
    m = _OUTPUT_LIMIT_RE.search(message)
    if not m:
        return False
    limit = int(m.group(1) or m.group(2))
    t = traits(model)
    if t.max_output == limit:
        return False
    t.max_output = limit
    return True


_STARVED_ERROR_RE = re.compile(r"could not finish the message because max_tokens", re.IGNORECASE)


def starved_error(body: Any) -> bool:
    """Whether a 400 says the output cap ran out before the reply finished —
    what a reasoning model returns instead of a `length` stop when its hidden
    reasoning spent the cap."""
    message, _ = _error_text(body)
    return bool(_STARVED_ERROR_RE.search(message))


def lower_effort(body: Any, sent: str) -> str | None:
    """The lowest effort a 400 says the model supports, or None when the 400 is
    not a refused effort."""
    message, _ = _error_text(body)
    refused = _EFFORT_REFUSED_RE.search(message)
    if not refused:
        return None
    value = refused.group(1)
    supported = _SUPPORTED_RE.search(message)
    if supported:
        allowed = set(re.findall(r"'([a-z]+)'", supported.group(1)))
        candidates = [e for e in EFFORT_ORDER if e in allowed]
    elif value in EFFORT_ORDER:
        candidates = list(EFFORT_ORDER[EFFORT_ORDER.index(value) + 1 :])
    else:
        candidates = []
    return next((e for e in candidates if e not in (value, sent)), None)
