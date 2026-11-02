"""The pure half of `loom decisions extract`: the candidate prompt, reading the model's
reply, the verbatim check, and sorting the code names a candidate mentions.

A candidate is kept only when its quote, its reasoning and every rejected option's reason
each sit inside one chunk the model read: what reaches an agent before an edit is the
document's own words.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from server.code.domain.anchor import mentions as code_mentions
from server.knowledge.domain.chunking import SEPARATOR, detect_sections
from server.memory.domain.corpus_synthesis import Window

# How a rejected option is stored: `<option> — rejected: <reason>`.
REJECTED = " — rejected: "
# A Markdown image, an HTML image tag, or a slide export's image placeholder.
_IMAGE_MARKUP = re.compile(r"!\[[^\]]*\]\([^)]*\)|<img\b[^>]*>|\[(?:image|picture)[^\]]*\]", re.I)
_LETTER = re.compile(r"[^\W\d_]")
_PR_NUMBER = re.compile(r"^(?:#|PR\s*#?)\d+$", re.I)
_SETTING_NAME = re.compile(r"^[a-z][a-z0-9]*(?:_[a-z0-9]+)+$")
_BARE_CAMEL = re.compile(r"^[A-Z][a-z0-9]+(?:[A-Z][A-Za-z0-9]*)+$")

SYSTEM = (
    "You extract design decisions from one part of a design document for a store that "
    "coding agents read before they edit the code the decision governs. Reply with one "
    'JSON object, {"decisions": [...]}, each item with the keys category, scenario, '
    "outcome, reasoning, alternatives, quote, is_constraint and mentions.\n"
    "- quote: the exact span of the text the decision rests on, copied character for "
    "character.\n"
    "- reasoning: why the outcome was chosen, an exact span of the text.\n"
    '- alternatives: the options the text rejects, each {"option": ..., "reason": ...}, '
    "the reason an exact span of the text.\n"
    "- category: design:<component>, lower case, so decisions about one component share "
    "it.\n"
    "- scenario: the specific situation that was decided, specific enough that another "
    "document's decision does not share it.\n"
    '- outcome: what was decided, short enough that "Decided <outcome> because '
    '<reasoning>." fits 220 characters.\n'
    "- is_constraint: true when the decision is a ban or an invariant code must keep.\n"
    "- mentions: the class, function and setting names and pull request numbers the "
    "text names, as written.\n"
    "Copy the document's words; never paraphrase a quote, a reasoning or a reason. Keep "
    "product names in quote only. Emit nothing for a section whose body is only a "
    'heading or an image. At most {max_per_section} items per section; {"decisions": '
    "[]} when the part records no decision."
)


# The most earlier decisions the prompt lists, and the characters of an outcome shown.
# Category and scenario are shown whole: the key is built from them.
MAX_KNOWN_DECISIONS = 30
_KNOWN_OUTCOME_CHARS = 160


def system_prompt(max_per_section: int, known: Sequence[tuple[str, str, str]] = ()) -> str:
    """The candidate prompt; `known` lists the decisions already recorded from this
    document as (category, scenario, outcome), whose wording a still-stated decision
    keeps, so a re-run reads the same keys and outcomes."""
    prompt = SYSTEM.replace("{max_per_section}", str(max_per_section))
    if not known:
        return prompt
    lines = []
    for category, scenario, outcome in known[:MAX_KNOWN_DECISIONS]:
        outcome = " ".join(outcome.split())
        if len(outcome) > _KNOWN_OUTCOME_CHARS:
            outcome = outcome[: _KNOWN_OUTCOME_CHARS - 1].rstrip() + "…"
        lines.append(" | ".join((" ".join(category.split()), " ".join(scenario.split()), outcome)))
    return (
        prompt + "\nDecisions already recorded from this document, as category | scenario | "
        "outcome; an outcome ending in … is shortened. When the part still states one, reuse "
        "its category and scenario exactly, and its outcome as far as it is shown:\n"
        + "\n".join(f"- {line}" for line in lines)
    )


def breadcrumb(section_path: tuple[str, ...]) -> str:
    """The section's heading path as the chunker writes it."""
    return SEPARATOR.join(section_path)


def user_prompt(window: Window) -> str:
    parts: list[str] = []
    last: tuple[str, ...] | None = None
    for c in window.chunks:
        if c.section_path != last:
            parts.append(f"[section: {breadcrumb(c.section_path) or '(untitled)'}]")
            last = c.section_path
        parts.append(c.text)
    return "\n\n".join(parts)


def image_only(body: str) -> bool:
    """A section body with no words once its images are removed: a heading over a picture."""
    return not _LETTER.search(_IMAGE_MARKUP.sub("", body))


@dataclass
class Candidate:
    category: str
    scenario: str
    outcome: str
    reasoning: str
    quote: str
    # (option, reason) pairs
    alternatives: list[tuple[str, str]] = field(default_factory=list)
    is_constraint: bool = False
    mentions: list[str] = field(default_factory=list)

    def rejected(self) -> list[str]:
        """The alternatives as decision.record stores them."""
        return [f"{option}{REJECTED}{reason}" for option, reason in self.alternatives]


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _list(value: Any) -> list[Any]:
    """A list field of the reply, or [] when the model wrote something else there."""
    return value if isinstance(value, list) else []


def _span(value: Any) -> str:
    """A verbatim span as the model wrote it: stripping it could make it match."""
    return value if isinstance(value, str) else ""


def _alternative(item: Any) -> tuple[str, str] | None:
    if not isinstance(item, dict):
        return None
    option, reason = _text(item.get("option")), _span(item.get("reason"))
    return (option, reason) if option and reason.strip() else None


def parse_candidates(reply: str) -> list[Candidate] | None:
    """The candidates of one model reply (its code fence already removed by the caller);
    None when the reply is not the asked-for JSON. An item missing a required field is
    skipped."""
    try:
        data = json.loads(reply)
    except (TypeError, ValueError):
        return None
    items = data.get("decisions") if isinstance(data, dict) else data
    if not isinstance(items, list):
        return None
    out: list[Candidate] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        alternatives = [a for a in map(_alternative, _list(item.get("alternatives"))) if a]
        mentions = [_text(m) for m in _list(item.get("mentions")) if _text(m)]
        cand = Candidate(
            category=_text(item.get("category")),
            scenario=_text(item.get("scenario")),
            outcome=_text(item.get("outcome")),
            reasoning=_span(item.get("reasoning")),
            quote=_span(item.get("quote")),
            alternatives=alternatives,
            is_constraint=item.get("is_constraint") is True,
            mentions=mentions,
        )
        if cand.category and cand.scenario and cand.outcome and cand.reasoning.strip():
            out.append(cand)
    return out


# The shortest span a decision may rest on: "We" or "the" is in every document, and a
# reasoning that short never leaves it, so its row would never retire. Characters, so a
# CJK span is held to the same floor.
MIN_SPAN_CHARS = 12


def verbatim_failure(cand: Candidate, texts: list[str]) -> str:
    """Why `cand` fails the verbatim check against the chunk texts the model read, or ''.
    Each span must sit inside one chunk: the join between two is not the document's. A
    span of the quote is in the quote's chunk, so the chunks alone decide."""

    def holds(span: str) -> bool:
        return any(span in t for t in texts)

    if len(cand.quote.strip()) < MIN_SPAN_CHARS:
        return "quote too short to rest a decision on"
    if not holds(cand.quote):
        return "quote is not in the document text"
    if len(cand.reasoning.strip()) < MIN_SPAN_CHARS:
        return "reasoning too short to rest a decision on"
    if not holds(cand.reasoning):
        return "reasoning is not in the document text"
    for option, reason in cand.alternatives:
        if len(reason.strip()) < MIN_SPAN_CHARS:
            return f"the reason given for rejecting {option!r} is too short to rest on"
        if not holds(reason):
            return f"the reason given for rejecting {option!r} is not in the document text"
    return ""


@dataclass(frozen=True)
class Mentions:
    """A candidate's mentions sorted by what can resolve them."""

    # (name, signal): `qualified` for a backticked or qualified name, `camelcase` for a
    # bare class name; each goes to code.symbol
    symbols: list[tuple[str, str]]
    # pull request numbers and setting names: not resolved, printed for the human
    unresolved: list[tuple[str, str]]


def sort_mentions(names: list[str], text: str, *, cap: int = 8) -> Mentions:
    """The mentions the document text names, by kind. A name the text does not contain is
    dropped: a file is linked only from what the document says."""
    symbols: list[tuple[str, str]] = []
    unresolved: list[tuple[str, str]] = []
    for raw in dict.fromkeys(names):
        name = raw.strip("`").strip()
        if not name or name not in text:
            continue
        if _PR_NUMBER.match(name):
            unresolved.append((name, "pr"))
        elif "::" in name or "." in name or f"`{name}`" in text:
            # Backticked first: `handle_decision_record` is code, though shaped like a
            # setting; one the index does not define is reported as a setting after all.
            if code_mentions(f"`{name}`"):
                symbols.append((name, "qualified"))
        elif looks_like_setting(name):
            unresolved.append((name, "setting"))
        elif _BARE_CAMEL.match(name):
            symbols.append((name, "camelcase"))
    return Mentions(symbols=symbols[:cap], unresolved=unresolved)


def looks_like_setting(name: str) -> bool:
    """`max_threads`: lower-case words joined by underscores."""
    return bool(_SETTING_NAME.match(name))


def _constructor(qualified_name: str) -> bool:
    """`DB::Foo::Foo`: a constructor, named like its class."""
    parts = re.split(r"::|\.", qualified_name)
    return len(parts) >= 2 and parts[-1] == parts[-2]


def definition_path(definitions: list[tuple[str, str]]) -> str:
    """The one file a symbol lookup's (path, qualified_name) definitions name, preferring
    the class over its constructor; '' when they name several files or none."""
    classes = [d for d in definitions if not _constructor(d[1])]
    paths = {path for path, _ in (classes or definitions) if path}
    return next(iter(paths)) if len(paths) == 1 else ""


def image_only_sections(content: str) -> list[str]:
    """The breadcrumbs of the document's sections with no words: a heading alone or over a
    picture, whose rationale no text carries. A heading with subsections is not one."""
    sections = detect_sections(content)
    out: list[str] = []
    for i, sec in enumerate(sections):
        nxt = sections[i + 1].path if i + 1 < len(sections) else ()
        has_children = len(nxt) > len(sec.path) and nxt[: len(sec.path)] == sec.path
        if sec.path and not has_children and image_only("\n".join(sec.lines)):
            out.append(breadcrumb(sec.path))
    return out


def quote_section(window: Window, quote: str) -> str:
    """The breadcrumb of the section the quote is in: the first chunk holding all of it,
    else the one holding its start, else the window's first section."""
    for c in window.chunks:
        if quote in c.text:
            return breadcrumb(c.section_path)
    head = quote[:80]
    for c in window.chunks:
        if head in c.text:
            return breadcrumb(c.section_path)
    return breadcrumb(window.chunks[0].section_path) if window.chunks else ""
