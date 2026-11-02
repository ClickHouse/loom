"""Render search hits the way `memory.ask` reads them.

On a ten-conversation QA run the same ten hits scored 61.0 with a
plain "use only the memories below" prompt and 68.0 with the ask
synthesizer's prompt on the same model: date- and key-labelled excerpts,
evidence ordered by how the question reads it, and instructions to
enumerate sets and trust the most recent copy. An integrator reading
`memory.search` with its own prompt leaves those points on the table, so
`memory.search(render="reader")` returns this block and the instructions
next to the hits. Pure; no I/O."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

_EXCERPT_CHARS = 2000


def _get(h: Any, name: str) -> Any:
    if isinstance(h, dict):
        return h.get(name)
    return getattr(h, name, None)


def _date_of(h: Any) -> str:
    """The date a hit reflects: the event date first (`temporal_anchor`),
    then when it became true (`valid_at`), then when it was written."""
    for attr in ("temporal_anchor", "valid_at", "created_at"):
        v = _get(h, attr)
        if v:
            txt = str(v).replace("T", " ")
            if len(txt) >= 16 and txt[11:16] not in ("00:00", ""):
                return txt[:16]
            return txt[:10]
    return "undated"


# What an external reader must say when the memories do not support an
# answer. memory.ask never reaches its prompt on an unsupported question:
# the retrieval gate abstains first. A reader handed the block has no gate,
# and on a ten-conversation QA run the bare synthesizer prompt
# answered 55% of adversarial questions the bench template abstained on.
READER_ABSTAIN_TEXT = "Not enough information in memory to answer."


def reader_instructions() -> str:
    """The synthesizer's system prompt plus the abstention rule an external
    reader needs because it sits after no retrieval gate."""
    from server.memory.app.ask.prompts import _SYNTH_SYSTEM

    return (
        f"{_SYNTH_SYSTEM} If none of the memories supports an answer to the question, "
        f"or the question presumes something the memories contradict or never state, "
        f"reply exactly: {READER_ABSTAIN_TEXT}"
    )


def render_reader_block(hits: list[Any], question: str = "", *, label_namespace: str = "") -> str:
    """One paragraph per hit: `[date] [memory_key] (type) excerpt`, in the
    order `memory.ask` would read them for this question (chronological for
    temporal questions, rank order otherwise). A hit from `label_namespace`
    names it beside its type: `(decision, code-acme)`."""
    if not hits:
        return ""
    from server.memory.app.ask.rendering import evidence_order

    # evidence_order reads attributes (it is written for hit models); a dict
    # hit from a transport or a test is wrapped so it orders the same way.
    objs = [SimpleNamespace(**h) if isinstance(h, dict) else h for h in hits]
    ordered, _mode = evidence_order(objs, None, question)
    keys = {str(_get(h, "memory_key") or "") for h in ordered}
    parts = []
    for h in ordered:
        key = str(_get(h, "memory_key") or "")
        mtype = str(_get(h, "memory_type") or "memory")
        if label_namespace and _get(h, "namespace") == label_namespace:
            mtype = f"{mtype}, {label_namespace}"
        text = str(_get(h, "content_excerpt") or "")[:_EXCERPT_CHARS]
        parts.append(f"[{_date_of(h)}] [{key}] ({mtype}) {text}{_restatement_note(h, keys)}")
    return "\n\n".join(parts)


def _restatement_note(h: Any, keys_in_block: set[str]) -> str:
    """A chunk or digest the fusion capped under
    the fact it cites (`score_components.cites_fact`) is served with a note
    naming that fact as the current value. The cap orders the rows; the
    reader's own rule ("trust the most recent") would still read a later-dated
    transcript line "You live in Ujjain, saved as `where_i_live`" over the
    edited `where_i_live` row.
    The note is added only when the cited row is in the served block."""
    comps = _get(h, "score_components")
    cited = str(comps.get("cites_fact") or "") if isinstance(comps, dict) else ""
    if not cited or cited not in keys_in_block:
        return ""
    return (
        f" (restates [{cited}] as it stood when this was said;"
        f" the [{cited}] row carries the current value)"
    )
