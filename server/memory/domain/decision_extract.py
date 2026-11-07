"""The pure half of `loom decisions extract`: the read prompt, reading the model's reply,
grounding each claim in the document's text, the support check's prompt and reply, and
sorting the code names a candidate mentions.

The model reads the whole document (or a window of whole sections when it is longer) and
writes each decision as a maintainer would: its outcome, its reasons and the options it
rejects, in its own concise words, each claim citing short quotes of the document. A
claim is kept only when one of its quotes is found in the text the model read,
whitespace, letter case and a few label lines aside (`find_span`; the quotes not found
are left out), and the support check, a second call over the same text, finds the claim
stated or directly implied by its quotes: no reason, option or fact reaches an agent
that the document does not give. A decision the document states with no reason is kept
with none.
"""

from __future__ import annotations

import json
import re
from collections.abc import Collection, Sequence
from dataclasses import dataclass, field, replace
from typing import Any

from server.code.domain.anchor import mentions as code_mentions
from server.code.domain.extract.lang_markdown import is_markdown_path
from server.knowledge.domain.chunking import SEPARATOR, Section, detect_sections
from server.memory.domain.corpus_synthesis import Window
from server.memory.domain.decision_schema import Stage
from server.memory.domain.decision_statement import (
    REJECTED,
    alternatives,
    collapse_ws,
    states_reason,
)

# An image: a Markdown image, inline (`![flow](flow.png)`) or by reference
# (`![diagram][fig1]`); an HTML `<img>` tag; or a slide export's placeholder, `[image]`,
# `[Image 3]`, `[image: ...]`, `[image_1]`, `[pictures]`. A shortcut reference
# (`![spill flow]`) is not read: it looks like a macro such as `vec![0; n]`.
# Not a placeholder: link text `[image tiles](tiles.md)`, a reference link `[image][1]`,
# a definition `[image]: x.png`, the label of `[the diagram][image]`, a shortcut link
# `[image tiles]`, text such as `[images are in the appendix]`, or a word `[imagine]`.
# Alt text and a placeholder stop at the next `[`, a tag at the next `<`, and a target
# never runs past the next `](`: a scan from every unclosed opener to the end of the text
# would cost quadratic time.
# One piece of a target `(...)`: none crosses a `)`, a line end outside a quoted title,
# or a `](`. A title holding `]` is read piece by piece, its line ends counted with the
# target's.
_TARGET_PIECE = r"""(?x:
    [^)\]\n"']++  # the destination and a title's text
  | \](?!\()      # a `]` that opens no target
  | "[^")\]]*+"   # a quoted title, which may span lines
  | '[^')\]]*+'   # the same, in single quotes
  | ["']          # a lone quote: `it's.png`, or an unclosed title `"see (a`
)"""
# A target: its pieces, with up to three line ends anywhere, as many as CommonMark allows
# (after `(`, between the destination and the title, before `)`).
_PAREN_TARGET = rf"\((?:{_TARGET_PIECE})*+(?:\n(?:{_TARGET_PIECE})*+){{0,3}}+\)"
_IMAGE_MARKUP = re.compile(
    rf"!\[[^\[\]]*+\]{_PAREN_TARGET}"
    r"|!\[[^\[\]]*+\]\[[^\]]*\]"
    r"|<img\b[^<>]*+>"
    r"|(?<!\])\[(?:image|picture)s?(?:[\s_]?\d+|:[^\[\]]*+)?\](?![(\[:])",
    re.I,
)
_LETTER = re.compile(r"[^\W\d_]")
_PR_NUMBER = re.compile(r"^(?:#|PR\s*#?)\d+$", re.I)
_SETTING_NAME = re.compile(r"^[a-z][a-z0-9]*(?:_[a-z0-9]+)+$")
_BARE_CAMEL = re.compile(r"^[A-Z][a-z0-9]+[A-Z][A-Za-z0-9]*$")

SYSTEM = (
    "You read a design document and list the design decisions it records, for a store "
    "that coding agents read before they edit the code a decision governs. An agent that "
    "reads a decision must not propose again an option the document rejected, must keep a "
    "constraint it states, and must be able to say why.\n"
    'Reply with one JSON object, {"decisions": [...]}, each item with the keys category, '
    "scenario, outcome, evidence, reasons, rejected, is_constraint, stage and mentions.\n"
    "- category: design:<component>, lower case, so decisions about one component share "
    "it.\n"
    "- scenario: the specific situation decided, one short clause, specific enough that "
    "another document's decision does not share it.\n"
    "- outcome: what was chosen, or how the system works, in your own concise words, "
    "without the reason; name the classes, functions, settings and values the document "
    "names.\n"
    "- evidence: 1-3 quotes of the document that state the outcome.\n"
    '- reasons: the reasons the document gives for the outcome, each {"text": ..., '
    '"evidence": [quotes]}: the cost, constraint, failure or measurement the choice rests '
    "on, in your own concise words. A reason may be in another section than the outcome "
    "(a problem statement, a measurement, a list of missing cases). [] when the document "
    "gives none: never invent one.\n"
    '- rejected: the options the document rejects, each {"option": ..., "reason": ..., '
    '"evidence": [quotes]}, the reason "" when the document gives none; the current or '
    "former approach a new design replaces is a rejected option of the new design.\n"
    "- is_constraint: true when the decision is a ban, a limit or an invariant code must "
    "keep.\n"
    "- stage: proposed when the document puts it forward without saying it was chosen and "
    "built (a proposal, an RFC, a plan, an option under discussion, future work, a "
    "follow-up); decided when the document says it was chosen or is how the system works. "
    "A decision in a proposal or an RFC (a [document: ...] line, when present, names the "
    "document) is proposed unless the text says it was built. When unsure, proposed.\n"
    "- mentions: the class, function, file and setting names and pull request numbers the "
    "document names for the decision, as written.\n"
    "Each quote is copied character for character from the document, 3 to 30 words on one "
    "line; a section heading is document text. Every reason and every rejected option "
    "has its own evidence. One item per choice: merge what the document says about one "
    "choice across its sections, and do not split a choice into the steps of its "
    "mechanism. A measurement, a summary or recap, an agenda or a benchmark result is no "
    "decision. Never give as the outcome what the document marks as former, replaced or "
    "superseded: the decision is what replaced it. A document that weighs options without "
    "choosing records the constraints it states and its proposals. At most "
    '{max_per_section} items whose evidence starts in one section; {"decisions": []} '
    "when the text records no decision."
)


def system_prompt(max_per_section: int, known: Sequence[tuple[str, str]] = ()) -> str:
    """The read prompt; `known` lists the decisions already recorded from this document
    as (category, scenario), whose key a still-recorded decision keeps, so a re-run
    revises the same rows. Only the key is shown, never the old outcome: the outcome is
    read from the text as it is now."""
    prompt = SYSTEM.replace("{max_per_section}", str(max_per_section))
    if not known:
        return prompt
    lines = [" | ".join((collapse_ws(c), collapse_ws(s))) for c, s in known]
    return (
        prompt + "\nDecisions already recorded from this document, as category | scenario. "
        "When the text still records one of them, as it was or changed, reuse its category "
        "and scenario exactly, and write its outcome and reasons as the text now gives "
        "them:\n" + "\n".join(f"- {line}" for line in lines)
    )


def breadcrumb(section_path: tuple[str, ...]) -> str:
    """The section's heading path as the chunker writes it."""
    return SEPARATOR.join(section_path)


# Not a setting: long enough to name any document; a runaway title adds little to each read.
TITLE_CHARS = 200


def user_prompt(window: Window, title: str) -> str:
    """The window's text under its section headings, after a line naming the document:
    a window of a proposal reads as a proposal even where its sentences say "we use"."""
    parts = [f"[document: {collapse_ws(title)[:TITLE_CHARS]}]"] if title.strip() else []
    last: tuple[str, ...] | None = None
    for c in window.chunks:
        if c.section_path != last:
            parts.append(f"[section: {breadcrumb(c.section_path) or '(untitled)'}]")
            last = c.section_path
        parts.append(c.text)
    return "\n\n".join(parts)


# Text beside an image up to this long is its caption, one line naming the picture: it
# carries no rationale, and a model asked about it invents one.
# not a setting: the definition of a caption, which decides the sections never sent
CAPTION_MAX_CHARS = 80


# A fenced code block or an inline code span.
_CODE = re.compile(r"```.*?```|~~~.*?~~~|`[^`\n]+`", re.S)
_MARKUP_CHARS = re.compile(r"[!\[\]<>]")


def _code_as_text(body: str) -> str:
    """`body` with the markup characters inside code blanked: `![d](f.png)` and `<img>`
    in code are text, never an image, and still count as text."""
    return _CODE.sub(lambda m: _MARKUP_CHARS.sub(" ", m.group(0)), body)


def no_words(body: str) -> bool:
    """No letters once its images are removed: a heading alone or over a picture."""
    return not _LETTER.search(_IMAGE_MARKUP.sub("", _code_as_text(body)))


# Markup around an image's caption that is no text a reader sees: an HTML tag, a link's
# target or reference label, a bare URL. A tag is lower case and on one line, its name
# followed by a space, `/` or `>`: `x < 10k and y > 80%` and `Option<A>` are text. Any
# name, not only inline ones: a badge often sits in a `<p>` or a `<div>`.
_HTML_TAG = re.compile(r"</?[a-z][a-z0-9-]*(?=[\s/>])[^<>\n]*>")
_LINK_TARGET = re.compile(rf"\]{_PAREN_TARGET}|\]\[[^\]]*\]")
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


def more_than_a_caption(body: str) -> bool:
    """`body` holds more text than a caption, images and link markup aside."""
    return len(caption_text(_code_as_text(body))) > CAPTION_MAX_CHARS


def image_only(body: str) -> bool:
    """A section body with no words (`no_words`), or with an image and no more text than
    a caption."""
    if no_words(body):
        return True
    return has_image(body) and not more_than_a_caption(body)


@dataclass(frozen=True)
class Claim:
    """A reason the document gives, in the model's words, and the quotes it rests on."""

    text: str
    evidence: tuple[str, ...]


@dataclass(frozen=True)
class Rejected:
    """An option the document rejects, why ('' when it says not), and the quotes."""

    option: str
    reason: str
    evidence: tuple[str, ...]


@dataclass
class Candidate:
    category: str
    scenario: str
    outcome: str
    # The quotes that state the outcome.
    evidence: list[str] = field(default_factory=list)
    reasons: list[Claim] = field(default_factory=list)
    alternatives: list[Rejected] = field(default_factory=list)
    is_constraint: bool = False
    mentions: list[str] = field(default_factory=list)
    # "proposed": put forward, not yet chosen or built.
    stage: Stage = "proposed"

    @property
    def quote(self) -> str:
        """The first quote of the outcome: where the decision is, in reports and links."""
        return self.evidence[0] if self.evidence else ""

    @property
    def reasoning(self) -> str:
        """The reasons as decision.record stores them; '' when the document gives none."""
        return "; ".join(r.text.rstrip(" .;") for r in self.reasons)

    def quotes(self) -> list[str]:
        """Every quote the candidate cites, outcome first."""
        out = list(self.evidence)
        for r in self.reasons:
            out += r.evidence
        for a in self.alternatives:
            out += a.evidence
        return out

    def rejected(self) -> list[str]:
        """The alternatives as decision.record stores them: `<option> — rejected: <reason>`,
        or the option alone when the document gives no reason."""
        return [
            f"{a.option}{REJECTED}{a.reason}" if a.reason else a.option for a in self.alternatives
        ]


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _list(value: Any) -> list[Any]:
    """A list field of the reply, or [] when the model wrote something else there."""
    return value if isinstance(value, list) else []


def _quotes(value: Any) -> tuple[str, ...]:
    return tuple(q for q in map(_text, _list(value)) if q)


def _claim(item: Any) -> Claim | None:
    if not isinstance(item, dict):
        return None
    text, evidence = _text(item.get("text")), _quotes(item.get("evidence"))
    return Claim(text, evidence) if text and evidence else None


def _rejected(item: Any) -> Rejected | None:
    if not isinstance(item, dict):
        return None
    option, evidence = _text(item.get("option")), _quotes(item.get("evidence"))
    return Rejected(option, _text(item.get("reason")), evidence) if option and evidence else None


def _stage(value: Any) -> Stage:
    """Only a plain "decided" reads as decided: a proposal served as made misleads more
    than a choice served as a proposal."""
    return "decided" if _text(value).lower() == "decided" else "proposed"


# A model that writes the statement's verb into the outcome: "Decided to use X",
# "Proposed: X". An outcome that opens with the word itself ("Proposed decisions are
# hidden") keeps it.
_LEADING_VERB = re.compile(r"^(?:decided|proposed)(?:\s+to\b|\s*:)\s*", re.I)


def parse_candidates(reply: str) -> list[Candidate] | None:
    """The candidates of one model reply (its code fence already removed by the caller);
    None when the reply is not the asked-for JSON. An item without a category, scenario,
    outcome or evidence is skipped; a reason or rejected option without evidence is
    left out."""
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
        cand = Candidate(
            category=_text(item.get("category")),
            scenario=_text(item.get("scenario")),
            outcome=_LEADING_VERB.sub("", _text(item.get("outcome"))),
            evidence=list(_quotes(item.get("evidence"))),
            reasons=[c for c in map(_claim, _list(item.get("reasons"))) if c],
            alternatives=[a for a in map(_rejected, _list(item.get("rejected"))) if a],
            is_constraint=item.get("is_constraint") is True,
            mentions=[_text(m) for m in _list(item.get("mentions")) if _text(m)],
            stage=_stage(item.get("stage")),
        )
        if cand.category and cand.scenario and cand.outcome and cand.evidence:
            out.append(cand)
    return out


# The shortest quote a claim may rest on: "We" or "the" is in every document, so a quote
# that short grounds nothing. Characters, so a CJK quote is held to the same floor.
MIN_SPAN_CHARS = 12


# The label lines a span reads across between two of its words.
# not a setting: part of what a label line is, which the grounding docs state
LABEL_LINES_MAX = 3
# Words of a ban or an invariant, and words that say a sentence rejects or contrasts
# what it names: a line holding one is never a label.
_BAN_WORDS = r"only|no|never|must|not|always|at\s+most|at\s+least"
_NEGATION_WORDS = (
    r"not|no|never|cannot|can['’]t|instead|rather|without|fail\w*|reject\w*|than"
    r"|avoid\w*|slow|over|against|versus|vs|unlike|replac\w*|favou?r\w*|prefer\w*"
    r"|outperform\w*|beats?|compar\w*|drop\w*"
)
# Whitespace other than a line break.
_H = r"[^\S\n]"
# What ends a sentence: a full stop, a question or exclamation mark, a semicolon, ASCII
# or full-width.
_SENTENCE_PUNCT = "[.!?;。！？；]"
# The end of a sentence, closing brackets, quotes and emphasis after the mark aside.
_SENTENCE_TAIL = rf"{_SENTENCE_PUNCT}[)\]\"'”’*_]*$"
_SENTENCE_END_WORD = re.compile(_SENTENCE_TAIL)
# A label line: one to three words on a line of their own, as a slide or diagram
# converted to text puts box labels between a sentence's two halves.
_LABEL_LINE = rf"{_H}*\S+(?:{_H}+\S+){{0,2}}{_H}*\n"
_BLANK_LINES = rf"(?:{_H}*\n)*"
# Between two words of a span: a line break, one to three label lines, blank lines
# around them, then the next word on its own line. As few label lines as reach the next
# word: a later line reading as it is not taken for a label. The lines read across are
# a group, which `_only_labels` checks.
_LABEL_GAP = rf"{_H}*\n({_BLANK_LINES}(?:{_LABEL_LINE}{_BLANK_LINES}){{1,{LABEL_LINES_MAX}}}?){_H}*"
# Words that change what a sentence says and are in neither list above.
_HEDGE_WORDS = (
    r"rarely|seldom|hardly|barely|scarcely|nor|neither|none|nothing|nobody|nowhere"
    r"|unless|except|but|although|though|however|yet"
)
# A title-like line that is still no label: structure (a list item, a bullet, fence,
# table row, heading, a line starting with `=` (an AsciiDoc heading), HTML tag, quote,
# definition), a sentence end, or a negation or hedge. A line with no letter or digit
# (an underline, a rule, a bare fence) is never a label (`_is_label`).
_NOT_A_LABEL = re.compile(
    rf"^(?:[-*+]\s|[•◦▪‣·‧]|[`~|#>:=<]|\d+[.)](?:\s|$))|{_SENTENCE_TAIL}"
    rf"|\b(?:{_BAN_WORDS}|{_NEGATION_WORDS}|{_HEDGE_WORDS})\b|n['’]t\b",
    re.I,
)


def _is_label(line: str) -> bool:
    """A label is title-like: its first letter or digit, after any markup (`**Proxy**`,
    `+3 nodes`), is an upper-case letter or a digit, which a line of wrapped prose
    continuing its sentence ("We keep the / old / index") and a line of a script without
    case never are; and it is no structure, sentence end or negation (`_NOT_A_LABEL`)."""
    first = next((ch for ch in line if ch.isalnum()), "")
    return (first.isupper() or first.isdigit()) and not _NOT_A_LABEL.search(line)


def _only_labels(lines: str | None) -> bool:
    """The lines a match read across between two words are all labels (or none were)."""
    return not lines or all(_is_label(x.strip()) for x in lines.splitlines() if x.strip())


def _span_match(span: str, texts: Sequence[str]) -> re.Match[str] | None:
    """The first text's match of `span`, as `find_span` reads it, or None."""
    words = span.split()
    if not words:
        return None
    # Atomic gaps, the next word right after the whitespace tried first: linear time.
    parts = [re.escape(words[0])]
    for i in range(1, len(words)):
        prev, w, last = words[i - 1], re.escape(words[i]), i == len(words) - 1
        end = r"(?!\w)" if last else r"(?=\s)"
        # A label sits inside a sentence: no label gap after a sentence's end, which
        # would join two sentences across a heading between them.
        label = "" if _SENTENCE_END_WORD.search(prev) else rf"|{_LABEL_GAP}{w}{end}"
        # The last word may still end inside a document word (a CJK span has no spaces).
        partial = rf"|\s+{w}" if last else ""
        parts.append(rf"(?>\s+{w}{end}{label}{partial})")
    pattern = re.compile("".join(parts), re.IGNORECASE)
    for text in texts:
        # Labels are checked after the match; a rejected match retries from the next start.
        start = 0
        while m := pattern.search(text, start):
            if all(map(_only_labels, m.groups())):
                return m
            start = m.start() + 1
    return None


def find_span(span: str, texts: Sequence[str]) -> str:
    """The first text's own span that reads as `span`, or '': each run of whitespace
    taken as one space, letter case ignored, and up to three label lines read across
    between two words, as a model copies a span from a converted slide. What is returned
    is the text's, labels included, so it is always a substring of the text. A label
    starts with an upper-case letter or a digit, so a line of a script without case is
    never read across. Not found: a label line whose first word reads as the next word
    ("The proxy" over "the nearest replica"), and a last word that would end inside a
    document word where a rejected label reading of its gap comes first ("cache plan"
    over "cache / plans / not / plan"): the label reading is tried before the partial
    end. A label is recognised by its shape and the word lists of `_NOT_A_LABEL`, so a
    short line of another kind that the sentence needs can still be read across."""
    m = _span_match(span, texts)
    return m.group(0) if m else ""


def _without_labels(span: str, texts: Sequence[str]) -> list[str] | None:
    """The pieces of the text's match of `span` between the label lines it read across
    (and the blank lines around them), or None when it is not found."""
    m = _span_match(span, texts)
    if not m:
        return None
    pieces, at = [], m.start()
    for n in range(1, len(m.groups()) + 1):
        if m.start(n) >= 0:
            pieces.append(m.string[at : m.start(n)])
            at = m.end(n)
    pieces.append(m.string[at : m.end()])
    return pieces


def document_span(span: str, texts: Sequence[str]) -> str:
    """The text's span that reads as `span` (`find_span`) as the document words it: its
    own words and case on one line, its label lines left out, or ''."""
    pieces = _without_labels(span, texts)
    return collapse_ws(" ".join(pieces)) if pieces else ""


def _grounded(quotes: Sequence[str], texts: Sequence[str]) -> tuple[str, ...]:
    """The quotes found in the text, each as the document words it (`document_span`); a quote
    shorter than MIN_SPAN_CHARS rests on nothing and is left out."""
    out = []
    for q in quotes:
        if len(collapse_ws(q)) < MIN_SPAN_CHARS:
            continue
        span = document_span(q, texts)
        if span and span not in out:
            out.append(span)
    return tuple(out)


UNGROUNDED = "none of the outcome's quotes is in the document text"


def ground(cand: Candidate, texts: list[str]) -> tuple[Candidate | None, list[str]]:
    """(`cand` with only the claims whose quotes are in the text, each quote the text's
    own; or None when none of the outcome's quotes is there) and what was left out: a
    reason or rejected option keeps the quotes found and is left out when none is."""
    evidence = _grounded(cand.evidence, texts)
    if not evidence:
        return None, [UNGROUNDED]
    notes: list[str] = []
    reasons = []
    for r in cand.reasons:
        if found := _grounded(r.evidence, texts):
            reasons.append(Claim(r.text, found))
        else:
            notes.append(f"reason {r.text!r} left out: none of its quotes is in the document text")
    rejected = []
    for a in cand.alternatives:
        if found := _grounded(a.evidence, texts):
            rejected.append(Rejected(a.option, a.reason, found))
        else:
            notes.append(
                f"rejected option {a.option!r} left out: none of its quotes is in the document text"
            )
    return replace(cand, evidence=list(evidence), reasons=reasons, alternatives=rejected), notes


SUPPORT_SYSTEM = (
    "You check claims extracted from a design document against the document. Each claim "
    "cites quotes of it. A claim is supported when a careful reader would agree that the "
    "quotes, read in the context of their sections and the rest of the document, state "
    "the claim or directly imply it: summarising, paraphrasing and combining the quotes "
    "is fine. A reason is supported when the document presents it as the problem, cost, "
    "limit or motivation the outcome answers, even without the word because. An option "
    "the document describes as the current or former approach, with its problems, beside "
    "the new design that replaces it, is an option the document rejects. A claim is "
    "unsupported when it adds what the document does not say: a reason, a cause, a "
    "number, a name or a rejected option. Whether the document decided the outcome or "
    "only proposes it is not checked here: judge only whether the document says what the "
    "claim says.\n"
    'Reply with one JSON object: {"claims": [{"id": ..., "why": <5 to 15 words>, '
    '"supported": true|false}]}, one item per claim id.'
)
# Added to SUPPORT_SYSTEM when the extractor holds decisions from an earlier reading.
STORED_NOTE = (
    "\nThe stored decisions were recorded from an earlier reading of the document. For "
    "each, still_stated is true when the document still says everything the stored "
    "statement says, wording aside, and false when the document changed or no longer "
    "says it; same_as is the number of the candidate (the number before the dot in "
    'its claim ids) that records the same decision, or null. Add "stored": [{"id": '
    '<stored id>, "why": <5 to 15 words>, "still_stated": true|false, "same_as": <number '
    "or null>}] to the reply, one item per stored id."
)


def support_system(stored: bool) -> str:
    """The support check's system prompt, with the stored decisions' part when the
    extractor holds some."""
    return SUPPORT_SYSTEM + (STORED_NOTE if stored else "")


def _claim_lines(i: int, cand: Candidate) -> list[dict[str, Any]]:
    """Candidate `i`'s claims, each with its id and quotes: `<i>.o` for the outcome,
    `<i>.r<j>` for a reason, `<i>.x<j>` for a rejected option."""
    head = cand.outcome[:200]
    # The stage is the read's to judge; the check reads what the claim says.
    out = [{"id": f"{i}.o", "claim": f"The document says: {cand.outcome}", "quotes": cand.evidence}]
    for j, r in enumerate(cand.reasons):
        claim = f"A reason the document gives for '{head}': {r.text}"
        out.append({"id": f"{i}.r{j}", "claim": claim, "quotes": list(r.evidence)})
    for j, a in enumerate(cand.alternatives):
        why = f", because {a.reason}" if a.reason else ""
        claim = f"The document rejects, in favour of '{head}': {a.option}{why}"
        out.append({"id": f"{i}.x{j}", "claim": claim, "quotes": list(a.evidence)})
    return out


def stored_statement(record: dict[str, Any]) -> str:
    """A stored decision as the support check reads it: its scenario, outcome, reasons and
    rejected options from the record, without the stage verb or a realized outcome, which
    no design document states."""
    scenario, outcome, reasoning = (
        collapse_ws(record.get(k) or "") for k in ("scenario", "outcome", "reasoning")
    )
    stated = f"{outcome} because {reasoning}" if states_reason(record) else outcome
    rejected = "; ".join(a.replace(REJECTED.rstrip(), " —") for a in alternatives(record))
    parts = [scenario, stated, f"Rejected: {rejected}" if rejected else ""]
    return ". ".join(p for p in parts if p)


def support_prompt(body: str, cands: Sequence[Candidate], stored: Sequence[tuple[str, str]]) -> str:
    """The support check's user prompt: the text the model read (`body`, as `user_prompt`
    writes it), each candidate's claims, and the stored decisions as (key, statement)."""
    claims = [line for i, c in enumerate(cands) for line in _claim_lines(i, c)]
    parts = [body, "[candidates]", json.dumps(claims, ensure_ascii=False)]
    if stored:
        listed = [{"id": key, "statement": collapse_ws(s)} for key, s in stored]
        parts += ["[stored decisions]", json.dumps(listed, ensure_ascii=False)]
    return "\n\n".join(parts)


@dataclass(frozen=True)
class Verdicts:
    """The support check's reply: the claim ids it supports, and each stored decision's
    (still stated, the candidate index that restates it or None)."""

    supported: frozenset[str]
    stored: dict[str, tuple[bool, int | None]]


def parse_support(reply: str, asked: Collection[str]) -> Verdicts | None:
    """The support check's reply; None when it is not the asked-for JSON. A claim the
    reply leaves out is unsupported; a stored decision it leaves out, or answers with
    anything but true or false, gets no verdict, and one whose id is not among the `asked`
    keys is ignored."""
    try:
        data = json.loads(reply)
    except (TypeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    supported = frozenset(
        str(v.get("id"))
        for v in _list(data.get("claims"))
        if isinstance(v, dict) and v.get("supported") is True
    )
    stored: dict[str, tuple[bool, int | None]] = {}
    for v in _list(data.get("stored")):
        still = v.get("still_stated") if isinstance(v, dict) else None
        if not isinstance(still, bool) or str(v.get("id")) not in asked:
            continue
        raw = v.get("same_as")
        digits = str(raw) if isinstance(raw, (int, str)) else ""
        # A candidate index is short; a longer number is no index (and past 4,300 digits
        # int() refuses it).
        index = int(digits) if digits.isdecimal() and len(digits) <= 6 else None
        stored[str(v.get("id"))] = (still, index)
    return Verdicts(supported, stored)


UNSUPPORTED = "the support check found the outcome not stated by its quotes"


def supported(i: int, cand: Candidate, v: Verdicts) -> tuple[Candidate | None, list[str]]:
    """Candidate `i` with only the claims the support check supports, or None when it
    does not support the outcome; and what was left out."""
    if f"{i}.o" not in v.supported:
        return None, [UNSUPPORTED]
    notes: list[str] = []
    reasons = []
    for j, r in enumerate(cand.reasons):
        if f"{i}.r{j}" in v.supported:
            reasons.append(r)
        else:
            notes.append(f"reason {r.text!r} left out: the support check found it not stated")
    rejected = []
    for j, a in enumerate(cand.alternatives):
        if f"{i}.x{j}" in v.supported:
            rejected.append(a)
        else:
            notes.append(
                f"rejected option {a.option!r} left out: the support check found it not stated"
            )
    return replace(cand, reasons=reasons, alternatives=rejected), notes


# A section that restates others: its quote is no decision, though its text stays in the
# window as a later decision's reason.
_RECAP_HEADING = re.compile(r"\b(?:recap|summary|background)\b", re.I)
RECAP = "the quote is in a recap, summary or background section"


def in_recap(section: str) -> bool:
    """A heading at any level of the breadcrumb names a recap, summary or background."""
    return bool(_RECAP_HEADING.search(section))


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
            # setting; one the index does not define, or only tests and pages define with a page
            # among them, is reported as a setting after all.
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


# Common test, mock and end-to-end layouts; one it misses keeps its link. Unlike
# is_test_unit_path, a module named `test_*` or `*Test` is code.
_TEST_FILE = re.compile(
    r"(?:^|/)(?:tests?|__tests__|__mocks__|mocks|testdata|testFixtures|integrationTest|e2e"
    r"|cypress)/"
    r"|\.(?:test|spec)\.[A-Za-z]+$|_(?:unit)?test\.[A-Za-z]+$|_mock\.go$|_spec\.rb$"
    r"|(?:^|/)(?:tests\.rs|tests\.py|conftest\.py|mock_\w+\.go)$"
)


def governs(path: str) -> bool:
    """A decision can govern the file: code, not a test that names the thing (a mocked op's
    key) or a Markdown heading that writes about it."""
    return not _TEST_FILE.search(path) and not is_markdown_path(path)


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
    no heading, counts when it holds a picture and at most a caption; its breadcrumb is
    ''."""
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
