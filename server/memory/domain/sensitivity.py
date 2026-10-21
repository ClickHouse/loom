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

import bisect
import json
import re
from collections.abc import Callable, Hashable, Iterable, Sequence
from typing import Any, Final

from server.memory.domain import chunk_shape

HIGH: Final = "high"
MEDIUM: Final = "medium"
LEVELS: Final = ("", "low", "medium", "high")

# A private-key block from its header to its closing line, or to the end of the text when
# that is not in it: the base64 body runs past a chunk, and every chunk it covers is held.
_PRIVATE_KEY_BLOCK = re.compile(
    r"-----BEGIN ((?:RSA |EC |OPENSSH |PGP )?)PRIVATE KEY-----.*?(-----END \1PRIVATE KEY-----|\Z)",
    re.S,
)
_PRIVATE_KEY_END = re.compile(r"-----END (?:RSA |EC |OPENSSH |PGP )?PRIVATE KEY-----")

_HIGH_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bsk-(?:proj-|ant-|lf-)?[A-Za-z0-9_-]{20,}"),  # OpenAI / Anthropic / Langfuse
    re.compile(r"\bpk-lf-[0-9a-fA-F-]{16,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),  # AWS access key id
    re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{30,}\b"),  # GitHub tokens
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{40,}\b"),
    re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"),  # Slack
    _PRIVATE_KEY_BLOCK,
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
    unescaped = _JSON_ESCAPE.sub(lambda m: _JSON_ESCAPES.get(m.group(1), m.group(1)), text)
    # Where each escape's one character sits in `unescaped`: every escape before a position
    # shortened the text by one, so the position in `text` is the position plus that count.
    decoded_at = [m.start() - i for i, m in enumerate(escapes)]

    def in_text(pos: int) -> int:
        return pos + bisect.bisect_left(decoded_at, pos)

    spans += [
        (in_text(m.start()), in_text(m.end()))
        for pat in _HIGH_PATTERNS
        for m in pat.finditer(unescaped)
    ]
    return spans


def overlaps_credential(source: str, ranges: list[tuple[int, int]]) -> list[bool]:
    """For each [start, end) range of `source` (a chunk's place in the text it was cut from),
    whether it overlaps a credential in `source` or its own text reads as one. A chunk cut
    through a key holds only its head or its tail, which does not read as a credential on its
    own; a chunk that starts inside a word can read as one where `source` does not."""
    spans = credential_spans(source)
    return [
        reads_high(source[start:end])
        or any(start < s_end and s_start < end for s_start, s_end in spans)
        for start, end in ranges
    ]


def _overlap(previous: str, chunk: str) -> int:
    """Length of the longest start of `chunk` that ends `previous`, between CHUNK_OVERLAP // 2
    and 2 * CHUNK_OVERLAP characters; 0 when none (a shorter match is a coincidence)."""
    longest = min(len(previous), len(chunk), 2 * chunk_shape.CHUNK_OVERLAP)
    shortest = chunk_shape.CHUNK_OVERLAP // 2
    for size in range(longest, shortest - 1, -1):
        if previous.endswith(chunk[:size]):
            return size
    return 0


# How far a key cut at a chunk boundary can reach on either side of it: a key shorter than a
# chunk, plus the characters the second chunk may repeat from the first.
_BOUNDARY_REACH = (  # not a setting: derived from the chunk shape
    chunk_shape.CHUNK_SIZE + 2 * chunk_shape.CHUNK_OVERLAP
)


def _cut_at_boundary(first: str, second: str) -> bool:
    """Whether a credential runs from `first` into the `second` that follows it. Only the text
    within `_BOUNDARY_REACH` of the boundary is read, so the cost does not grow with the chunks."""
    repeat = _overlap(first, second)
    tail = first[-_BOUNDARY_REACH:]
    text = tail + second[repeat : repeat + _BOUNDARY_REACH]
    # A span that touches both chunks; one wholly inside either is found in that chunk alone.
    return any(
        start < len(tail) and end > len(tail) - repeat for start, end in credential_spans(text)
    )


# Distinct pairs of neighbouring chunks one call reads. A call that sends one version of each
# session has at most one pair per chunk; past this, a pair is held without being read, so
# many versions of a session cannot make the check's cost grow with the square of the count.
_MAX_JOINED_PAIRS = 512  # not a setting: security bound


def _opens_private_key(text: str) -> bool:
    """Whether `text` holds a private-key header with no closing line after it."""
    return any(not m.group(2) for m in _PRIVATE_KEY_BLOCK.finditer(text))


def split_credentials(chunks: Sequence[tuple[Hashable, int, str]]) -> list[bool]:
    """For each `(session, chunk_index, content)` chunk, whether it overlaps a credential in
    its own text or in the text it makes joined with a chunk of its session at the next index.
    Chunks with a gap in `chunk_index` are not joined. A key shorter than a chunk is cut by at
    most one boundary, so pairs of neighbouring chunks are enough. Pairs with the same
    contents are read once, and both chunks of a pair past `_MAX_JOINED_PAIRS` are held.

    A private-key block runs past a chunk: after a chunk that opens one and does not close
    it, every chunk of its session is held up to and including the one with the closing
    line. A closing line cut at a boundary is not seen, so the rest of the session is held."""
    held = [bool(credential_spans(content)) for _, _, content in chunks]
    closes = [bool(_PRIVATE_KEY_END.search(content)) for _, _, content in chunks]
    key_open = [_opens_private_key(content) for _, _, content in chunks]
    by_index: dict[tuple[Hashable, int], list[int]] = {}
    for i, (session, index, _) in enumerate(chunks):
        by_index.setdefault((session, index), []).append(i)
    cut: dict[tuple[str, str], bool] = {}
    # Whether a private-key header cut at the pair's boundary leaves a block open there, which
    # neither chunk shows alone. A pair held unread counts as one.
    opened: dict[tuple[str, str], bool] = {}
    for (session, index), firsts in by_index.items():
        for a in firsts:
            for b in by_index.get((session, index + 1), []):
                pair = (chunks[a][2], chunks[b][2])
                if pair not in cut:
                    read = len(cut) < _MAX_JOINED_PAIRS
                    cut[pair] = not read or _cut_at_boundary(*pair)
                    boundary = pair[0][-_BOUNDARY_REACH:] + pair[1][:_BOUNDARY_REACH]
                    opened[pair] = cut[pair] and (not read or _opens_private_key(boundary))
                if cut[pair]:
                    held[a] = held[b] = True
                if opened[pair] and not closes[b]:
                    key_open[b] = True
    # In index order, so a block carried into a chunk is carried on from it.
    for session, index in sorted(by_index, key=lambda k: k[1]):
        if any(key_open[a] for a in by_index[(session, index)]):
            for b in by_index.get((session, index + 1), []):
                held[b] = True
                key_open[b] = key_open[b] or not closes[b]
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
