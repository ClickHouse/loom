"""A session brief as text: the framed markdown a resuming session reads, its
one-line summary, and the JSON its sections are stored as."""

from __future__ import annotations

import json
from dataclasses import dataclass

from server.memory.domain.session_brief import (
    RENDER_CHAR_CAP,
    BriefEvent,
    BriefSections,
    _clip,
    previous_for_prompt,
)

BRIEF_FRAME = (
    "(A saved brief of earlier work — where it stood when saved, condensed from "
    "that session. It is a starting point, not a source of facts: check the files "
    "and the tree before relying on a line, and answer what/where/who questions "
    "with loom_search.)"
)


@dataclass(frozen=True)
class RenderMeta:
    brief_key: str
    revision: int
    updated_at: str = ""
    trigger: str = ""
    project: str = ""
    branch: str = ""
    head_sha: str = ""
    sessions: int = 0
    events_total: int = 0
    source_chars: int = 0
    latest_session: str = ""


def _header(meta: RenderMeta) -> list[str]:
    where = " ".join(
        x
        for x in (
            f"project {meta.project}" if meta.project else "",
            f"branch {meta.branch}" if meta.branch else "",
            f"at {meta.head_sha[:12]}" if meta.head_sha else "",
        )
        if x
    )
    saved = f"saved {meta.updated_at}" if meta.updated_at else "saved"
    via = f" by {meta.trigger}" if meta.trigger else ""
    lines = [f'# Session brief "{meta.brief_key}" — revision {meta.revision}, {saved}{via}']
    if where:
        lines.append(where)
    if meta.events_total:
        kb = meta.source_chars / 1024
        n = meta.sessions
        lines.append(
            f"condensed from {meta.events_total:,} events ({kb:,.0f} KB) of "
            f"{n} session{'s' if n != 1 else ''}"
        )
    lines.append(BRIEF_FRAME)
    return lines


def _body(s: BriefSections) -> list[tuple[str, list[str]]]:
    """(heading, lines) in reading order; the order the trim removes from
    is the reverse of this list."""
    out: list[tuple[str, list[str]]] = []
    if s.goal:
        out.append(("Goal", [s.goal]))
    if s.status:
        out.append(("Status", [s.status]))
    if s.next_steps:
        out.append(("Next steps", [f"{i}. {x}" for i, x in enumerate(s.next_steps, 1)]))
    if s.open_questions:
        out.append(("Open questions", [f"- {x}" for x in s.open_questions]))
    if s.decisions:
        out.append(
            (
                "Decisions (keep unless told otherwise)",
                [f"- {d.what}" + (f" — because {d.why}" if d.why else "") for d in s.decisions],
            )
        )
    if s.done:
        out.append(("Done", [f"- {d.what}" + (f" ({d.ref})" if d.ref else "") for d in s.done]))
    if s.dead_ends:
        out.append(
            (
                "Tried and dropped (do not retry)",
                [f"- {d.tried}" + (f" — {d.why_not}" if d.why_not else "") for d in s.dead_ends],
            )
        )
    if s.constraints:
        out.append(("Rules the user set", [f"- {x}" for x in s.constraints]))
    if s.files:
        out.append(("Files", [f"- {f.path}" + (f" — {f.role}" if f.role else "") for f in s.files]))
    if s.pointers:
        out.append(("Pointers", [f"- {x}" for x in s.pointers]))
    return out


def render_brief(s: BriefSections, meta: RenderMeta, *, char_cap: int = RENDER_CHAR_CAP) -> str:
    """The text a model reads. Under `char_cap`: the lowest sections lose
    their oldest lines first (pointers, files, rules, dead ends, decisions);
    goal, status and next steps are never trimmed away."""
    head = _header(meta)
    body = _body(s)
    detail = (
        f"Detail: session.brief_get(brief_key={meta.brief_key!r}, revision=N) reads an "
        f"earlier revision"
        + (
            f"; session {meta.latest_session} holds each tool call as an observation"
            if meta.latest_session and meta.latest_session != "-"
            else ""
        )
        + "."
    )

    def text() -> str:
        parts = ["\n".join(head)]
        for heading, lines in body:
            if lines:
                parts.append(f"{heading}:\n" + "\n".join(lines))
        parts.append(detail)
        return "\n\n".join(parts)

    out = text()
    protected = {"Goal", "Status", "Next steps"}
    while len(out) > char_cap:
        victim = next((b for b in reversed(body) if b[0] not in protected and b[1]), None)
        if victim is None:
            break
        victim[1].pop(0)
        out = text()
    if len(out) > char_cap:
        out = out[: char_cap - 1] + "…"
    return out


def one_line(text: str, cap: int) -> str:
    """`text` flattened to one line of at most `cap` characters."""
    return _clip(text, cap)


def sections_json(s: BriefSections) -> str:
    return json.dumps(s.model_dump(), ensure_ascii=False, separators=(",", ":"))


def keep_client_summary(
    prev: BriefSections | None, sections: BriefSections, events: list[BriefEvent]
) -> BriefSections:
    """The newest client compaction summary among `events`, verbatim, else the
    one the brief already holds. The merge builds new sections, so this is the
    one place the field is carried."""
    latest = next((e for e in reversed(events) if e.kind == "summary" and e.text.strip()), None)
    if latest is not None:
        text, at = latest.text.strip()[:16000], latest.ts
    elif prev is not None:
        text, at = prev.client_summary, prev.client_summary_at
    else:
        text, at = "", ""
    return sections.model_copy(update={"client_summary": text, "client_summary_at": at})


def with_client_summary(rendered: str, sections: BriefSections) -> str:
    """`rendered` with the client's last compaction summary below it."""
    if not sections.client_summary:
        return rendered
    when = f" ({sections.client_summary_at})" if sections.client_summary_at else ""
    return (
        f"{rendered}\n\nThe client's own summary at its last compaction{when}, verbatim — "
        f"written by the session's model with every tool result in view; the brief above "
        f"is newer where they differ:\n\n{sections.client_summary}"
    )


def sections_from_json(raw: str) -> BriefSections:
    try:
        data = json.loads(raw or "{}")
        return BriefSections.model_validate(data if isinstance(data, dict) else {})
    except (ValueError, TypeError):
        return BriefSections()
    except Exception:  # noqa: BLE001 — a stored row from a newer shape reads as empty
        return BriefSections()


def evidence_text(prev: BriefSections | None, events: list[BriefEvent], condensed: str) -> str:
    """Everything a grounded file or pointer may come from: the condensed
    window the model saw, the raw event paths and texts (a path clipped out
    of a line is still evidence), and the previous brief."""
    parts = [condensed]
    parts.extend(e.path for e in events if e.path)
    parts.extend(
        e.text for e in events if e.kind in ("user", "assistant", "command", "error", "note")
    )
    if prev is not None:
        parts.append(previous_for_prompt(prev))
    return "\n".join(parts)
