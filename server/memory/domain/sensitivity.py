"""Sensitivity tier detection.

A memory row carries `sensitivity` in {'' , 'low', 'medium', 'high'}. The
caller can set it; when it does not, the value is classified here with
deterministic regexes — no LLM, no network:

  high    credential shapes: API keys with a known prefix, AWS access key
          ids, private-key blocks, JWTs, `password=` / `secret:` assignments
          followed by a long token. Also payment and identity numbers: a card
          number that passes the Luhn check, a US Social Security number, and
          a PIN, passcode, CVV or security code stated with its value. A
          'high' row is never embedded and is readable by key only, with an
          audit row per read.
  medium  contact and account details: an email address, a phone number, or
          an IBAN that passes its mod-97 check. Embedded and searchable, but
          labelled so a pack or an export can treat it.
  ''      everything else (unset). Rows are not labelled 'low' by the
          detector; 'low' is a caller's explicit statement.

The key and token patterns are the same family `scripts/gates/check_no_secrets.py`
uses to keep credentials out of the repo; the card, SSN and PIN patterns are not
repo secrets and live here only. A memory row holding a live key or a card number
must never be pulled into a prompt by similarity, which is what embedding it would
do. Every pattern is a linear regex pass, and the Luhn and mod-97 checks run on a
bounded number of windows of what a pattern matched, so the work stays linear in the
text.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from typing import Any, Final

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

# A run of digit groups joined by single spaces or dashes, not after `+` (an international
# phone number). A card number is 13-19 digits of such a run, on group boundaries, so a
# card pasted with its expiry or CVV ("4111 1111 1111 1111 12/29") or after another
# number is still found; `_has_card` tries each window of up to six groups.
_DIGIT_GROUPS = re.compile(r"(?<![\d+])\d+(?:[ -]\d+)*")
_CARD_MAX_GROUPS = 6
# Leading digits of the card networks: Amex/Diners 3, Visa 4, Mastercard 51-55 and 22-27,
# Discover/UnionPay 6. A number starting any other way (0, 1, 7, 8, 9, 20, 21, 28, 29:
# epoch milliseconds, yyyymmdd dates) is never a card.
_CARD_PREFIXES = ("3", "4", "5", "6", "22", "23", "24", "25", "26", "27")
# A US Social Security number written ddd-dd-dddd, without the never-issued ranges:
# area 000, 666 or 9xx, group 00, serial 0000.
_SSN = re.compile(r"(?<![\d-])(?!000|666|9)\d{3}-(?!00)\d{2}-(?!0000)\d{4}(?![\d-])")
# A PIN, passcode, CVV/CVC or security code stated with its 3-8 digit value. Every label
# takes the value after `is`, `was`, `:`, `=` or `#` ("PIN is 4471", "cvv: 123"). All but a
# bare `pin`, which is also a verb and a connector pin, take it directly too ("CVV 123",
# "pin code 0042"), unless an `of` follows ("passcode 100 of the batch" names a position).
# Each whitespace run has its own anchor, so a long run of spaces is read once.
_STATED_PIN = re.compile(
    r"(?i)\b(?:pin(?: code| number)?|passcode|cvv2?|cvc2?|security code)"
    r"(?:\s+(?:is|was)(?:\s*[:=])?|\s*[:=#])\s*\d{3,8}(?!\d)"
    r"|\b(?:pin code|pin number|passcode|cvv2?|cvc2?|security code)\s+\d{3,8}(?!\d)(?!\s+of\b)"
)
# An IBAN: country code, two check digits, then the BBAN in groups of four (spaced or
# not) with a short last group; 15-34 characters once the spaces are gone.
_IBAN = re.compile(r"\b[A-Z]{2}\d{2}(?: ?[A-Z0-9]{4}){2,7}(?: ?[A-Z0-9]{1,3})?\b")


def _luhn_ok(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = int(ch)
        if i % 2 == 1:
            d = d * 2 - 9 if d > 4 else d * 2
        total += d
    return total % 10 == 0


def _has_card(text: str) -> bool:
    """Whether a window of 13-19 digits on group boundaries of a digit run has a card
    network's leading digits and passes the Luhn check."""
    for m in _DIGIT_GROUPS.finditer(text):
        groups = re.split(r"[ -]", m.group())
        for i in range(len(groups)):
            digits = ""
            for group in groups[i : i + _CARD_MAX_GROUPS]:
                digits += group
                if len(digits) > 19:
                    break
                if len(digits) >= 13 and digits.startswith(_CARD_PREFIXES) and _luhn_ok(digits):
                    return True
    return False


def _is_iban(candidate: str) -> bool:
    """ISO 13616: move the first four characters to the end, letters become 10-35, and the
    number is 1 mod 97."""
    if not 15 <= len(candidate) <= 34:
        return False
    moved = candidate[4:] + candidate[:4]
    return int("".join(str(int(ch, 36)) for ch in moved)) % 97 == 1


def _has_iban(text: str) -> bool:
    """An IBAN in `text`. A spaced match can take in an uppercase word after it
    ("... 1332 EUR"), so its trailing groups are dropped one at a time."""
    for m in _IBAN.finditer(text):
        groups = m.group().split(" ")
        if any(_is_iban("".join(groups[:end])) for end in range(len(groups), 0, -1)):
            return True
    return False


def _has_high_number(text: str) -> bool:
    """A card number, an SSN or a stated PIN in `text`."""
    return bool(_SSN.search(text) or _STATED_PIN.search(text)) or _has_card(text)


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


def detect_sensitivity(text: str) -> str:
    """Classify `text`: 'high' for a credential, card, SSN or stated PIN, 'medium' for
    contact details or an IBAN, '' otherwise. Order matters — a line with both is 'high'."""
    if not text:
        return ""
    if any(pat.search(text) for pat in _HIGH_PATTERNS) or _has_high_number(text):
        return HIGH
    if any(pat.search(text) for pat in _MEDIUM_PATTERNS) or _has_iban(text):
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
    """Whether a stored value holds a high shape (a credential, card number, SSN or stated
    PIN), read decoded (`detector_text`), whatever the autodetect setting. For a value; raw
    stored text (a chunk, a passage) goes through `reads_high`, which also undoes JSON
    escapes."""
    return detect_sensitivity(detector_text(value)) == HIGH


# A JSON string escape, decoded once and in order (so `\\n`, an escaped backslash then `n`,
# stays a backslash and an `n`).
_JSON_ESCAPE = re.compile(r'\\(["\\/bfnrt])')
_JSON_ESCAPES = {"n": "\n", "t": "\t", "r": "\r", "b": "\b", "f": "\f"}


def reads_high(text: str) -> bool:
    """Whether `text` holds a high shape, whatever the autodetect setting: the rule for
    text copied from a row (a digest's key facts, an observation sent to a model, a raw
    chunk), which has no label of its own or carries one a later sweep may not have set
    yet. Text holding JSON (a chunk of an object value) is read with its string escapes
    undone too, where a token after `\\n` or a quoted password keeps its word boundary."""
    if detect_sensitivity(text) == HIGH:
        return True
    unescaped = _JSON_ESCAPE.sub(lambda m: _JSON_ESCAPES.get(m.group(1), m.group(1)), text)
    return unescaped != text and detect_sensitivity(unescaped) == HIGH


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
