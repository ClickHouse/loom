"""Evidence packing for bounded agent context.

Retrieval returns candidates; agents need compact evidence. This module groups
hits by source, deduplicates citations, preserves source diversity, and renders
bounded bullets for context.pack and future search/evidence surfaces.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from server.memory.domain.context_layout import label_part, one_item


@dataclass(frozen=True)
class EvidenceItem:
    citation: str
    text: str
    source: str = "fact"
    group: str = ""
    score: float = 0.0
    temporal_anchor: str = ""
    conflict: bool = False
    # Who wrote it (` by=…` on the line, from author_label); "" where the plane has no
    # writer of its own (knowledge, conversations cite their source instead).
    author: str = ""
    # The keys a digest compressed, so a summary says what it derives from.
    derived_from: tuple[str, ...] = ()
    # `changed@<commit>` when the memory's linked code changed, was not found again or
    # went (` code_state=…` on the line); "" otherwise.
    code: str = ""


@dataclass(frozen=True)
class PackedEvidence:
    rendered: str
    citations: list[str]
    source_count: int
    truncated: bool
    groups: list[str] = field(default_factory=list)


def _trim(text: str, char_budget: int) -> tuple[str, bool]:
    if len(text) <= char_budget:
        return text, False
    if char_budget <= 20:
        return text[:char_budget], True
    return text[: char_budget - 16].rstrip() + "\n...[truncated]", True


def _derived(item: EvidenceItem) -> str:
    keys = list(item.derived_from)
    if not keys:
        return ""
    more = f"+{len(keys) - 3}" if len(keys) > 3 else ""
    return f" derived_from={','.join(label_part(k) for k in keys[:3])}{more}"


def pack_evidence(
    items: list[EvidenceItem],
    *,
    char_budget: int,
    max_per_group: int = 3,
) -> PackedEvidence:
    """Render a bounded, source-diverse evidence list.

    Sorts by score descending, then lets each source group contribute up to
    `max_per_group` items before filling the remaining budget. This prevents
    one verbose transcript or document from crowding out other evidence planes.
    Order stays relevance-descending; the event date rides each line as a label.
    """

    if char_budget <= 0 or not items:
        return PackedEvidence("", [], 0, False, [])

    deduped: list[EvidenceItem] = []
    seen_citations: set[str] = set()
    for item in sorted(items, key=lambda i: -float(i.score or 0.0)):
        citation = item.citation.strip()
        text = item.text.strip()
        if not citation or not text or citation in seen_citations:
            continue
        seen_citations.add(citation)
        deduped.append(item)

    group_counts: dict[str, int] = {}
    primary: list[EvidenceItem] = []
    overflow: list[EvidenceItem] = []
    for item in deduped:
        group = item.group or item.source or "fact"
        count = group_counts.get(group, 0)
        if count < max_per_group:
            primary.append(item)
            group_counts[group] = count + 1
        else:
            overflow.append(item)
    ordered = primary + overflow

    lines: list[str] = []
    citations: list[str] = []
    groups: list[str] = []
    truncated = False
    for item in ordered:
        group = item.group or item.source or "fact"
        marker = " conflict=true" if item.conflict else ""
        date = f" date={item.temporal_anchor[:10]}" if item.temporal_anchor else ""
        code = f" code_state={item.code}" if item.code else ""
        labels = f"source={item.source}{item.author}{_derived(item)}{date}{marker}{code}"
        line = one_item(f"- [{label_part(item.citation)} {labels}] {item.text}")
        tentative = "\n".join([*lines, line])
        if len(tentative) > char_budget:
            truncated = True
            break
        lines.append(line)
        citations.append(item.citation)
        if group not in groups:
            groups.append(group)

    rendered, trim_truncated = _trim("\n".join(lines), char_budget)
    return PackedEvidence(
        rendered=rendered,
        citations=citations,
        source_count=len(citations),
        truncated=truncated or trim_truncated,
        groups=groups,
    )
