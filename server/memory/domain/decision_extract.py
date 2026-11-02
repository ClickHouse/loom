"""The pure half of `loom decisions extract`: the candidate prompt, reading the model's
reply, the verbatim and restatement checks, and sorting the code names a candidate
mentions.

A candidate is kept only when its quote, its reasoning and every rejected option's reason
each sit inside one chunk the model read, whitespace aside, and its reasoning says more
than its outcome: what reaches an agent before an edit is the document's own reason.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from typing import Any

from server.code.domain.anchor import mentions as code_mentions
from server.knowledge.domain.chunking import SEPARATOR, Section, detect_sections
from server.memory.domain.corpus_synthesis import Window
from server.memory.domain.decision_statement import collapse_ws
from server.memory.domain.lemmatize import snowball_stem_tokens

# How a rejected option is stored: `<option> — rejected: <reason>`.
REJECTED = " — rejected: "
# An image: a Markdown image, inline (`![flow](flow.png)`) or by reference
# (`![diagram][fig1]`); an HTML `<img>` tag; or a slide export's placeholder, `[image]`,
# `[Image 3]`, `[image: ...]`, `[image_1]`, `[pictures]`. A shortcut reference
# (`![spill flow]`) is not read: it looks like a macro such as `vec![0; n]`.
# Not a placeholder: link text `[image tiles](tiles.md)`, a reference link `[image][1]`,
# a definition `[image]: x.png`, the label of `[the diagram][image]`, a shortcut link
# `[image tiles]`, text such as `[images are in the appendix]`, or a word `[imagine]`.
_IMAGE_MARKUP = re.compile(
    r"!\[[^\]]*\]\([^)]*\)"
    r"|!\[[^\]]*\]\[[^\]]*\]"
    r"|<img\b[^>]*>"
    r"|(?<!\])\[(?:image|picture)s?(?:[\s_]?\d+|:[^\]]*)?\](?![(\[:])",
    re.I,
)
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
    "- reasoning: the reason the text gives for the choice (the cost, constraint or "
    "failure it avoids), an exact span of the text. Never the decision restated in other "
    "words: a span that only says what was chosen is no reasoning, and a decision whose "
    "text gives no reason is not emitted.\n"
    '- alternatives: the options the text rejects, each {"option": ..., "reason": ...}, '
    "the reason an exact span of the text.\n"
    "- category: design:<component>, lower case, so decisions about one component share "
    "it.\n"
    "- scenario: the specific situation that was decided, specific enough that another "
    "document's decision does not share it.\n"
    "- outcome: what was chosen, without the reason (that is the reasoning), short "
    'enough that "Decided <outcome> because <reasoning>." fits 220 characters.\n'
    "- is_constraint: true when the decision is a ban or an invariant code must keep.\n"
    "- mentions: the class, function and setting names and pull request numbers the "
    "text names, as written.\n"
    "Copy the document's words; never paraphrase a quote, a reasoning or a reason. Keep "
    "product names in quote only. Emit nothing for a section whose body is only a "
    "heading, an image, or an image with its caption, and nothing for a section that "
    "sums up or recaps other sections. Never give as the outcome what the text marks as "
    "former, replaced or superseded (an old default, a previous behaviour): the decision "
    "is what replaced it. At most {max_per_section} items per section; "
    '{"decisions": []} when the part records no decision.'
)


# How many earlier decisions the prompt lists at most, and the characters of an outcome shown.
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
        outcome = collapse_ws(outcome)
        if len(outcome) > _KNOWN_OUTCOME_CHARS:
            outcome = outcome[: _KNOWN_OUTCOME_CHARS - 1].rstrip() + "…"
        lines.append(" | ".join((collapse_ws(category), collapse_ws(scenario), outcome)))
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


# Text beside an image up to this long is its caption, one line naming the picture: it
# carries no rationale, and a model asked about it invents one.
CAPTION_MAX_CHARS = 80


# A fenced code block or an inline code span.
_CODE = re.compile(r"```.*?```|~~~.*?~~~|`[^`\n]+`", re.S)
_MARKUP_CHARS = re.compile(r"[!\[\]<>]")


def _code_as_text(body: str) -> str:
    """`body` with the markup characters inside code blanked: `vec![0; n]` and
    `<img>` in code are text, never an image, and still count as text."""
    return _CODE.sub(lambda m: _MARKUP_CHARS.sub(" ", m.group(0)), body)


def no_words(body: str) -> bool:
    """No letters once its images are removed: a heading alone or over a picture."""
    return not _LETTER.search(_IMAGE_MARKUP.sub("", _code_as_text(body)))


# Markup around an image's caption that is no text a reader sees: an HTML tag, a link's
# target or reference label, a bare URL. A tag is lower case and on one line, its name
# followed by a space, `/` or `>`: `x < 10k and y > 80%` and `Option<A>` are text. Any
# name, not only inline ones: a badge often sits in a `<p>` or a `<div>`.
_HTML_TAG = re.compile(r"</?[a-z][a-z0-9-]*(?=[\s/>])[^<>\n]*>")
_LINK_TARGET = re.compile(r"\]\([^)]*\)|\]\[[^\]]*\]")
_URL = re.compile(r"https?://\S+")
# What a link around an image leaves once the image is gone: `[ ]`.
_EMPTY_BRACKETS = re.compile(r"\[\s*\]")


def caption_text(body: str) -> str:
    """The text a reader sees beside the images: images, HTML tags, link targets and
    reference labels, the brackets a link around an image leaves, and bare URLs removed,
    so a badge wrapped in a link is no text."""
    text = _HTML_TAG.sub(" ", _IMAGE_MARKUP.sub(" ", body))
    text = _EMPTY_BRACKETS.sub(" ", _LINK_TARGET.sub("]", text))
    return collapse_ws(_URL.sub(" ", text))


def has_image(body: str) -> bool:
    """`body` holds an image outside its code."""
    return bool(_IMAGE_MARKUP.search(_code_as_text(body)))


def image_only(body: str) -> bool:
    """A section body with no words (`no_words`), or with an image and no more text than
    a caption."""
    if no_words(body):
        return True
    return has_image(body) and len(caption_text(_code_as_text(body))) <= CAPTION_MAX_CHARS


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


def _alternative(item: Any) -> tuple[str, str] | None:
    if not isinstance(item, dict):
        return None
    option, reason = _text(item.get("option")), _text(item.get("reason"))
    return (option, reason) if option and reason else None


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
            reasoning=_text(item.get("reasoning")),
            quote=_text(item.get("quote")),
            alternatives=alternatives,
            is_constraint=item.get("is_constraint") is True,
            mentions=mentions,
        )
        if cand.category and cand.scenario and cand.outcome and cand.reasoning:
            out.append(cand)
    return out


# The shortest span a decision may rest on: "We" or "the" is in every document, and a
# reasoning that short never leaves it, so its row would never retire. Characters, so a
# CJK span is held to the same floor.
MIN_SPAN_CHARS = 12


def find_span(span: str, texts: Sequence[str]) -> str:
    """The first text's own span that reads as `span` with each run of whitespace taken
    as one space, or '': a model copying a span drops line breaks and doubled spaces."""
    words = span.split()
    if not words:
        return ""
    pattern = re.compile(r"\s+".join(map(re.escape, words)))
    for text in texts:
        if m := pattern.search(text):
            return m.group(0)
    return ""


def verbatim_failure(cand: Candidate, texts: list[str]) -> str:
    """Why `cand` fails the verbatim check against the chunk texts the model read, or ''.
    Each span must sit inside one chunk: the join between two is not the document's. A
    span of the quote is in the quote's chunk, so the chunks alone decide."""

    def holds(span: str) -> bool:
        return bool(find_span(span, texts))

    def short(span: str) -> bool:
        # Measured as the document's span reads, whitespace runs one space.
        return len(collapse_ws(span)) < MIN_SPAN_CHARS

    if short(cand.quote):
        return "quote too short to rest a decision on"
    if not holds(cand.quote):
        return "quote is not in the document text"
    if short(cand.reasoning):
        return "reasoning too short to rest a decision on"
    if not holds(cand.reasoning):
        return "reasoning is not in the document text"
    for option, reason in cand.alternatives:
        if short(reason):
            return f"the reason given for rejecting {option!r} is too short to rest on"
        if not holds(reason):
            return f"the reason given for rejecting {option!r} is not in the document text"
    return ""


def document_spans(cand: Candidate, texts: list[str]) -> Candidate:
    """`cand` with each span replaced by the text's own, line breaks and all, for a
    candidate that passed the verbatim check: what is stored is the document's."""
    return replace(
        cand,
        quote=find_span(cand.quote, texts),
        reasoning=find_span(cand.reasoning, texts),
        alternatives=[(option, find_span(reason, texts)) for option, reason in cand.alternatives],
    )


# Words that say a choice was made, or join a sentence, rather than give a reason.
_FILLER_WORDS = (
    "a an the and or of to in on at for with as is are was were be been being has have had "
    "it its this that these those we our us they will would should must can so decide "
    "chose chosen choose use used using instead because since decision pick picked opt "
    "opted select selected"
)
# Compared as Snowball stems, so `spilling` meets `spill`, `stored` meets `store`, and
# every form of a filler word is one: `deciding`, `chooses`, `used`.
_FILLER_STEMS = frozenset(snowball_stem_tokens(_FILLER_WORDS.split()))
_WORD = re.compile(r"\w+")


def _content_words(text: str) -> set[str]:
    return set(snowball_stem_tokens(_WORD.findall(text.lower()))) - _FILLER_STEMS


RESTATED = "reasoning only restates the outcome: it has no word the outcome lacks"


def restates_outcome(cand: Candidate) -> bool:
    """The reasoning adds nothing to the outcome: every word of it, case, punctuation,
    plural and tense endings and the filler words aside, is a word of the outcome. Such
    a span is the decision in the document's words, not the reason for it."""
    return _content_words(cand.reasoning) <= _content_words(cand.outcome)


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
    """The breadcrumbs of the document's sections whose own text carries no rationale: a
    heading alone, over a picture, or over a picture and a caption of up to
    CAPTION_MAX_CHARS. A heading alone over its subsections is not one; a heading over a
    picture and then subsections is. The text above the first heading, or a document with
    no heading, counts when it holds a picture; its breadcrumb is ''."""
    sections = detect_sections(content) or [Section(path=(), depth=0, start=0, lines=[content])]
    out: list[str] = []
    for i, sec in enumerate(sections):
        nxt = sections[i + 1].path if i + 1 < len(sections) else ()
        has_children = len(nxt) > len(sec.path) and nxt[: len(sec.path)] == sec.path
        body = "\n".join(sec.lines)
        if image_only(body) and (has_image(body) or (sec.path and not has_children)):
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
