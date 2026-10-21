"""LLM-free additions after the parse: rows the model left out of a turn
(preferences, event clauses, costs, age), citation repair, and code facets.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import replace

from server.code.domain.facets import _MAX_FACET_ITEMS, extract_code_facets
from server.memory.app.extract.models import ExtractedFact
from server.memory.app.extract.turns import _message_stamp
from server.memory.domain.extraction_citations import repair_citations


def _recover_preferences(
    facts: list[ExtractedFact], messages: Sequence[Mapping[str, object]] | None
) -> list[ExtractedFact]:
    """A first-person preference the model left out of every fact of its turn
    becomes a `user_preference_*` row."""
    if not facts or not messages:
        return facts
    from server.memory.domain.extraction_preferences import recover_stated_preferences

    def _make(key: str, value: str, idx: int) -> ExtractedFact:
        return ExtractedFact(
            memory_key=key, value=value, tags=["preference"], source_message_indices=[idx]
        )

    return list(recover_stated_preferences(facts, messages, _make))


def _recover_rows(
    facts: list[ExtractedFact], messages: Sequence[Mapping[str, object]] | None
) -> list[ExtractedFact]:
    """The rows the model left out of a turn: preferences, then event clauses
    and stated costs, then the user's age."""
    return _recover_age(
        _recover_specifics(_recover_preferences(facts, messages), messages), messages
    )


def _recover_age(
    facts: list[ExtractedFact], messages: Sequence[Mapping[str, object]] | None
) -> list[ExtractedFact]:
    """A first-person age no fact of its turn carries becomes the `user_age`
    row, dated to the turn."""
    if not facts or not messages:
        return facts
    from server.memory.domain.extraction_attributes import recover_stated_age

    stamps = [_message_stamp(m) for m in messages]

    def _row(key: str, value: str, idx: int) -> ExtractedFact:
        return ExtractedFact(memory_key=key, value=value, source_message_indices=[idx])

    return list(recover_stated_age(facts, messages, lambda i: stamps[i][:10], _row))


def _recover_specifics(
    facts: list[ExtractedFact], messages: Sequence[Mapping[str, object]] | None
) -> list[ExtractedFact]:
    """A dropped start/finish clause and a dropped stated cost, each as its own
    row."""
    if not facts or not messages:
        return facts
    from server.memory.domain.extraction_specifics import recover_dropped_specifics

    def _row(key: str, value: str, idx: int) -> ExtractedFact:
        return ExtractedFact(memory_key=key, value=value, source_message_indices=[idx])

    return list(recover_dropped_specifics(facts, messages, make_event=_row, make_cost=_row))


def _repair(
    facts: list[ExtractedFact], messages: Sequence[Mapping[str, object]] | None
) -> list[ExtractedFact]:
    """Citation repair (`extraction_citations.repair_citations`)."""
    if not messages:
        return facts
    return repair_citations(facts, messages)


def _apply_code_facets(
    facts: list[ExtractedFact],
    messages: list[dict[str, object]],
    *,
    from_turns: bool,
) -> list[ExtractedFact]:
    """Populate `files` + `concepts` on each fact from its value and the
    source messages it cites. Deterministic, LLM-free, additive: a fact
    with no code content keeps empty arrays. Never mutates the fact value.

    Facets are drawn from the fact value PLUS the specific source messages
    the extractor cited (source_message_indices) — a path mentioned in the
    transcript but paraphrased out of the fact value still gets linked.
    Without `from_turns` (an operator's extraction policy is in force) only
    the value counts: the model may have left the rest out because of it.
    """
    if not facts:
        return facts
    out: list[ExtractedFact] = []
    changed = False
    for f in facts:
        surfaces = [f.value]
        cited = (f.source_message_indices or []) if from_turns else []
        for idx in cited:
            if 0 <= idx < len(messages):
                surfaces.append(str(messages[idx].get("content") or ""))
        files: list[str] = []
        concepts: list[str] = []
        seen_f: set[str] = set()
        seen_c: set[str] = set()
        for surface in surfaces:
            sf, sc = extract_code_facets(surface)
            for p in sf:
                if p.lower() not in seen_f and len(files) < _MAX_FACET_ITEMS:
                    seen_f.add(p.lower())
                    files.append(p)
            for c in sc:
                if c.lower() not in seen_c and len(concepts) < _MAX_FACET_ITEMS:
                    seen_c.add(c.lower())
                    concepts.append(c)
        if files or concepts:
            out.append(replace(f, files=files, concepts=concepts))
            changed = True
        else:
            out.append(f)
    return out if changed else facts
