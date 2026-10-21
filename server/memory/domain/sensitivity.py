"""Sensitivity tier detection.

A memory row carries `sensitivity` in {'' , 'low', 'medium', 'high'}. The
caller can set it; when it does not, the value is classified here with
deterministic regexes — no LLM, no network:

  high    credential shapes: API keys with a known prefix, AWS access key
          ids, private-key blocks, JWTs, `password=` / `secret:` assignments
          followed by a long token. A 'high' row is never embedded and is
          readable by key only, with an audit row per read.
  medium  contact details: an email address or a phone number. Embedded and
          searchable, but labelled so a pack or an export can treat it.
  ''      everything else (unset). Rows are not labelled 'low' by the
          detector; 'low' is a caller's explicit statement.

The high patterns are the same family `scripts/gates/check_no_secrets.py` uses to
keep credentials out of the repo; a memory row holding a live key must never
be pulled into a prompt by similarity, which is what embedding it would do.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Hashable, Iterable, Sequence
from typing import Any, Final

from server.memory.domain import chunk_shape

HIGH: Final = "high"
MEDIUM: Final = "medium"
LEVELS: Final = ("", "low", "medium", "high")

_HIGH_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bsk-(?:proj-|ant-|lf-)?[A-Za-z0-9_-]{20,}"),  # OpenAI / Anthropic / Langfuse
    re.compile(r"\bpk-lf-[0-9a-fA-F-]{16,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),  # AWS access key id
    re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{30,}\b"),  # GitHub tokens
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{40,}\b"),
    re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"),  # Slack
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |PGP )?PRIVATE KEY-----"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"),  # JWT
    re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"),  # Google API key
    # `password=…`, `api_key: …`, `secret := …`, `token = …` followed by a
    # token-shaped value of 8+ non-space characters.
    re.compile(
        r"(?i)\b(?:password|passwd|pwd|api[_-]?key|secret(?:[_-]?key)?|access[_-]?token|"
        r"auth[_-]?token|bearer|client[_-]?secret|private[_-]?key)\b\s*[:=]\s*[\"']?[^\s\"',;]{8,}"
    ),
)

_MEDIUM_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"),  # email
    # Phone numbers: E.164 (+14155550123) or grouped national forms with at
    # least 10 digits — separators allowed, so "415-555-0123" and
    # "(415) 555 0123" match but a 4-digit year or a 6-digit count does not.
    re.compile(r"(?<![\w.])(?:\+?\d{1,3}[\s.-]?)?\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4}(?![\w.])"),
)


# --- Protected-attribute classes --------------------------------------------
#
# A memory-enhanced agent can lean toward whichever group its user's memory
# encodes, and scrubbing explicit words is necessary but not sufficient
# (proxies survive). Loom does
# not scrub. It LABELS: a row whose value mentions a protected attribute or a
# common proxy gets an `attribute:<class>` tag at write time, so the profile
# fold can leave it out, an operator can count it, and a later gate can act on
# it. Deterministic regexes, no LLM; a lower bound by construction, since
# vocabulary lists miss proxies.
ATTRIBUTE_TAG_PREFIX: Final = "attribute:"

_ATTRIBUTE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "gender",
        re.compile(
            r"(?i)\b(?:male|female|woman|women|man|men|gender|non-?binary|transgender|"
            r"husband|wife|boyfriend|girlfriend|actress|waitress|mother|father|"
            r"pregnan\w*|maternity|paternity)\b"
        ),
    ),
    (
        "religion",
        re.compile(
            r"(?i)\b(?:religio\w*|christian\w*|catholic|protestant|muslim|islam\w*|jewish|"
            r"judaism|hindu\w*|buddhis\w*|sikh\w*|atheis\w*|church|mosque|synagogue|temple|"
            r"prayer\w*|ramadan|lent|sabbath|kosher|halal)\b"
        ),
    ),
    (
        "health",
        re.compile(
            r"(?i)\b(?:mental health|depress\w*|anxiety|therap\w*|diagnos\w*|medication|"
            r"disab\w*|wheelchair|chronic|illness|cancer|diabet\w*|hiv|autis\w*|adhd|"
            r"bipolar|surgery|hospitali[sz]\w*)\b"
        ),
    ),
    (
        "age",
        re.compile(
            r"(?i)\b(?:\d{1,2}[- ]years?[- ]old|aged \d{1,2}|elderly|retire\w*|senior citizen|"
            r"teenager|millennial|boomer|gen[- ]z|born in (?:19|20)\d\d)\b"
        ),
    ),
    (
        "family",
        re.compile(
            r"(?i)\b(?:married|divorc\w*|widow\w*|single (?:mother|father|parent)|"
            r"children|kids|newborn|toddler|caregiver|custody)\b"
        ),
    ),
    (
        "income",
        re.compile(
            r"(?i)\b(?:salary|income|wealth\w*|low-income|poverty|welfare|benefits claim\w*|"
            r"bankrupt\w*|credit score|mortgage)\b"
        ),
    ),
    (
        "ethnicity",
        re.compile(
            r"(?i)\b(?:ethnic\w*|racial|race|immigra\w*|nationality|hispanic|latin[oa]x?|"
            r"asian|african|caucasian|indigenous|native american|refugee|visa status|"
            r"citizenship)\b"
        ),
    ),
    (
        "sexuality",
        re.compile(r"(?i)\b(?:sexual orientation|gay|lesbian|bisexual|queer|lgbt\w*|same-sex)\b"),
    ),
    (
        "politics",
        re.compile(
            r"(?i)\b(?:political\w*|conservative|liberal|socialist|republican|democrat\w*|"
            r"union member\w*|voted? for)\b"
        ),
    ),
)


def attribute_classes(text: str) -> list[str]:
    """Protected-attribute classes the text mentions, in a fixed order.
    Bounded work: nine regexes over at most the first 4,000 characters."""
    if not text:
        return []
    head = text[:4000]
    return [name for name, pat in _ATTRIBUTE_PATTERNS if pat.search(head)]


def attribute_tags(text: str, existing: Iterable[str] = ()) -> list[str]:
    """`attribute:<class>` tags for `text` that `existing` does not carry.
    Empty when LOOM_MEMORY_ATTRIBUTE_TAGGING is off."""
    from server.settings import settings

    if not bool(settings.memory_attribute_tagging):
        return []
    have = {str(t).strip().lower() for t in existing}
    return [
        ATTRIBUTE_TAG_PREFIX + c
        for c in attribute_classes(text)
        if ATTRIBUTE_TAG_PREFIX + c not in have
    ]


def _holds_credential(text: str) -> bool:
    return any(pat.search(text) for pat in _HIGH_PATTERNS)


def detect_sensitivity(text: str) -> str:
    """Classify `text`: 'high' for a credential shape, 'medium' for contact
    details, '' otherwise. Order matters — a line with both is 'high'."""
    if not text:
        return ""
    if _holds_credential(text):
        return HIGH
    for pat in _MEDIUM_PATTERNS:
        if pat.search(text):
            return MEDIUM
    return ""


# A line no pattern matches across: `;` ends the assignment shape's token class, and the
# phone shape allows only one space, dot or dash between its groups.
_SCALAR_SEP = "\n;\n"


def detector_text(value: Any) -> str:
    """Decoded text of a stored value for the detector: a JSON string is parsed, and each key
    and scalar is its own line, so a token after a newline keeps its word boundary. The lines
    are joined by `_SCALAR_SEP`, which no pattern crosses, so two scalars never read as one
    (`Password:` then the next key, or `256`, `512`, `1024` as a phone number). Iterative, so
    a deeply nested value cannot stop a worker pass."""
    if isinstance(value, str):
        text = value
        try:
            value = json.loads(text)
        except (ValueError, RecursionError):
            return text
    lines: list[str] = []
    stack: list[Any] = [value]
    while stack:
        v = stack.pop()
        if isinstance(v, dict):
            for k, x in reversed(list(v.items())):
                stack.append(x)
                stack.append(str(k))
        elif isinstance(v, list | tuple):
            stack.extend(reversed(v))
        elif v is not None:
            lines.append(str(v))
    return _SCALAR_SEP.join(lines)


def value_reads_high(value: Any) -> bool:
    """Whether a stored value holds a credential shape, read decoded (`detector_text`), whatever
    the autodetect setting. For a value; raw stored text (a chunk, a passage) goes through
    `reads_high`, which also undoes JSON escapes."""
    return detect_sensitivity(detector_text(value)) == HIGH


# A JSON string escape, decoded once and in order (so `\\n`, an escaped backslash then `n`,
# stays a backslash and an `n`).
_JSON_ESCAPE = re.compile(r'\\(["\\/bfnrt])')
_JSON_ESCAPES = {"n": "\n", "t": "\t", "r": "\r", "b": "\b", "f": "\f"}


def reads_high(text: str) -> bool:
    """Whether `text` holds a credential shape, whatever the autodetect setting: the rule for
    text copied from a row (a digest's key facts, an observation sent to a model, a raw
    chunk), which has no label of its own or carries one a later sweep may not have set
    yet. Text holding JSON (a chunk of an object value) is read with its string escapes
    undone too, where a token after `\\n` or a quoted password keeps its word boundary."""
    if not text:
        return False
    if _holds_credential(text):
        return True
    unescaped = _JSON_ESCAPE.sub(lambda m: _JSON_ESCAPES.get(m.group(1), m.group(1)), text)
    return unescaped != text and _holds_credential(unescaped)


def credential_spans(text: str) -> list[tuple[int, int]]:
    """The [start, end) character ranges of `text` that read as a credential, by the rule
    `reads_high` applies: as written, and with JSON string escapes undone."""
    spans = [m.span() for pat in _HIGH_PATTERNS for m in pat.finditer(text)]
    escapes = list(_JSON_ESCAPE.finditer(text))
    if not escapes:
        return spans
    # Build the unescaped text with, for each of its characters, where it starts in `text`.
    parts: list[str] = []
    starts: list[int] = []
    pos = 0
    for m in escapes:
        parts.append(text[pos : m.start()])
        starts.extend(range(pos, m.start()))
        parts.append(_JSON_ESCAPES.get(m.group(1), m.group(1)))
        starts.append(m.start())
        pos = m.end()
    parts.append(text[pos:])
    starts.extend(range(pos, len(text) + 1))
    unescaped = "".join(parts)
    spans += [
        (starts[m.start()], starts[m.end()])
        for pat in _HIGH_PATTERNS
        for m in pat.finditer(unescaped)
    ]
    return spans


def overlaps_credential(source: str, ranges: list[tuple[int, int]]) -> list[bool]:
    """For each [start, end) range of `source` (a chunk's place in the text it was cut from),
    whether it overlaps a credential in `source`. A chunk cut through a key holds only its
    head or its tail, which does not read as a credential on its own."""
    spans = credential_spans(source)
    return [
        any(start < s_end and s_start < end for s_start, s_end in spans) for start, end in ranges
    ]


def _overlap(previous: str, chunk: str) -> int:
    """The length of the longest start of `chunk` that ends `previous`: the characters a
    chunk repeats from the one before it (`chunk_shape.CHUNK_OVERLAP`, less the whitespace a chunk
    is trimmed of). The chunker repeats at most that many characters; the search
    stops at twice that, a margin that still keeps the scan short and leaves a longer
    repeated run as new text. A match shorter than half the overlap is a coincidence,
    not a repeat (an imported chunk need not overlap at all), so it counts as none and
    the boundary keeps every character."""
    longest = min(len(previous), len(chunk), 2 * chunk_shape.CHUNK_OVERLAP)
    shortest = chunk_shape.CHUNK_OVERLAP // 2
    for size in range(longest, shortest - 1, -1):
        if previous.endswith(chunk[:size]):
            return size
    return 0


def split_credentials(chunks: Sequence[tuple[Hashable, int, str]]) -> list[bool]:
    """For each `(session, chunk_index, content)` chunk, whether it overlaps a credential in
    its session's text, rebuilt by joining the session's chunks in `chunk_index` order with
    each one's overlap with the one before it dropped: a key cut at a chunk boundary reads
    as one only when joined. A gap in `chunk_index` starts a new text, so chunks that were
    never neighbours are not joined."""
    held = [False] * len(chunks)
    sessions: dict[Hashable, list[int]] = {}
    for i, (session, _, _) in enumerate(chunks):
        sessions.setdefault(session, []).append(i)
    for positions in sessions.values():
        positions.sort(key=lambda i: chunks[i][1])
        runs: list[list[int]] = []
        for i in positions:
            if runs and chunks[i][1] == chunks[runs[-1][-1]][1] + 1:
                runs[-1].append(i)
            else:
                runs.append([i])
        for run in runs:
            source = ""
            ranges: list[tuple[int, int]] = []
            previous = ""
            for i in run:
                content = chunks[i][2]
                start = len(source) - _overlap(previous, content)
                source += content[len(source) - start :]
                ranges.append((start, len(source)))
                previous = content
            for i, overlaps in zip(run, overlaps_credential(source, ranges), strict=True):
                held[i] = overlaps
    return held


# Spare rows a read asks for, so a passage it holds back for reading as a credential does
# not leave the page short.
HELD_MARGIN = 2  # not a setting: algorithm shape, sized to how rarely a row is held


def without_credentials[T](
    items: Iterable[T], text: Callable[[T], str], limit: int | None = None
) -> list[T]:
    """`items`, in order, whose text does not read as a credential, the first `limit` of
    them. The text is checked only as far as `limit` reaches."""
    kept: list[T] = []
    for item in items:
        if limit is not None and len(kept) >= limit:
            break
        if not reads_high(text(item)):
            kept.append(item)
    return kept


def resolve_sensitivity(requested: str | None, text: str) -> str:
    """The caller's explicit level wins; an unset level is detected from the
    value when `memory_sensitivity_autodetect` is on, else stays ''."""
    level = (requested or "").strip().lower()
    if level in LEVELS and level:
        return level
    from server.settings import settings

    if not bool(settings.memory_sensitivity_autodetect):
        return ""
    return detect_sensitivity(text or "")


def max_sensitivity(levels: Iterable[str | None]) -> str:
    """The highest tier among `levels` ('' < low < medium < high); unknown
    strings count as ''."""
    best = 0
    for lv in levels:
        v = (lv or "").strip().lower()
        if v in LEVELS:
            best = max(best, LEVELS.index(v))
    return LEVELS[best]


def inherited_sensitivity(source_levels: Iterable[str | None], text: str) -> str:
    """Label for a row synthesised FROM other rows (a consolidated topic
    memory, a merged canonical row): never lower than its most sensitive
    source, and never lower than what the synthesised text itself contains.
    A summary that keeps an email address keeps the 'medium' label with it."""
    return max_sensitivity([max_sensitivity(source_levels), detect_sensitivity(text or "")])


SENSITIVITY_TAG_PREFIX: Final = "sensitivity:"
_CONTACT_INTENT = re.compile(
    r"\b(?:reach|contact|e-?mail(?:s|ed|ing)?|phone|get (?:a )?hold of|"
    r"call (?:him|her|them|me|us|the)|"
    r"how (?:do|can) i (?:reach|contact|get)|"
    r"who (?:do|should) i (?:call|email|contact))\b",
    re.IGNORECASE,
)


def is_high(level: object, tags: object) -> bool:
    """High by the sensitivity column or by the `sensitivity:high` tag memory.set adds."""
    if str(level or "").strip().lower() == HIGH:
        return True
    if not isinstance(tags, list):
        return False
    return SENSITIVITY_TAG_PREFIX + HIGH in {str(t).strip().lower() for t in tags}


def sensitivity_tags(level: str | None, existing: Iterable[str] = ()) -> list[str]:
    """`sensitivity:<level>` tag for a labelled row, so the label rides
    `loom.memory_embeddings.tags` (which has no sensitivity column) and the
    ranking can read it. Empty for unlabelled / low rows."""
    lv = (level or "").strip().lower()
    if lv not in (MEDIUM, HIGH):
        return []
    tag = SENSITIVITY_TAG_PREFIX + lv
    have = {str(t).strip().lower() for t in existing}
    return [] if tag in have else [tag]


def contact_intent(query: str) -> bool:
    """Does the question ask for a way to reach someone? Then a labelled
    contact detail is the answer, not collateral, and the ranking must not
    demote it."""
    return bool(_CONTACT_INTENT.search(query or ""))
