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
do. Every pattern is a linear regex pass; the Luhn check runs on each 15- or 16-digit
group and on at most sixteen grouped windows per run, and the mod-97 check on at most 55
runs per match, so the work stays linear in the text.
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

# The patterns below are ASCII-only (`re.ASCII`), as RE2 is in the sweep's coarse filter,
# so a row the write path labels is a row the sweep can select.
#
# A run of digit groups joined by single spaces or dashes, not after `+` (an international
# phone number). A card number is one ungrouped group of 15 (Amex) or 16 digits, or
# consecutive groups in a printed card grouping with at most three short groups (1-4
# digits) around it, at most two of them after it. So a card pasted with its expiry and
# CVV ("4111 1111 1111 1111 0429 123") or after a date ("2024-01-15 4111 ...") is still
# found, and two phone numbers side by side (3-3-4 groups), a table of eight or more
# numbers, or a 19-digit hash are not.
_DIGIT_GROUPS = re.compile(r"(?<![\d+])\d+(?:[ -]\d+)*", re.ASCII)
# Group lengths of a printed card: 16 and 19 digits in fours, Amex 4-6-5, Diners 4-6-4.
_CARD_GROUPINGS = ((4, 4, 4, 4), (4, 4, 4, 4, 3), (4, 6, 5), (4, 6, 4))


def _numbers(first: int, last: int) -> tuple[str, ...]:
    return tuple(str(n) for n in range(first, last + 1))


# Leading digits and lengths of the card networks. A number with other leading digits or
# another length (epoch milliseconds, yyyymmdd dates, a 15-digit IMEI, which always passes
# Luhn) is never a card.
_CARD_NETWORKS: tuple[tuple[tuple[str, ...], tuple[int, ...]], ...] = (
    (("4",), (16, 19)),  # Visa
    (_numbers(51, 55) + _numbers(2221, 2720), (16,)),  # Mastercard
    (("34", "37"), (15,)),  # Amex
    (_numbers(300, 305) + ("3095", "36", "38", "39"), (14,)),  # Diners
    (_numbers(3528, 3589), (16, 19)),  # JCB
    (("6011", "62", *_numbers(644, 649), "65"), (16, 19)),  # Discover, UnionPay
)
# A US Social Security number written ddd-dd-dddd, without the never-issued ranges:
# area 000, 666 or 9xx, group 00, serial 0000.
_SSN = re.compile(r"(?<![\d-])(?!000|666|9)\d{3}-(?!00)\d{2}-(?!0000)\d{4}(?![\d-])", re.ASCII)
# A PIN, passcode, CVV/CVC or security code stated with its value ("PIN is 4471",
# "CVV 123"). Two trade-offs: `pin` and `pin code` take 3-5 digits, since "PIN: 560001" is
# an Indian postal code; and a connector pin written as an assignment ("GPIO pin: 115")
# reads as a PIN, the price of reading "PIN: 4471" as one.
_PIN_VALUE_AFTER = r"(?:\s+(?:is|was)(?:\s*[:=])?|\s*[:=])\s*"
_LONG_PIN_LABEL = r"(?:pin number|passcode|cvv2?|cvc2?|security code)"
_STATED_PIN = re.compile(
    rf"(?i)\b{_LONG_PIN_LABEL}{_PIN_VALUE_AFTER}\d{{3,8}}(?!\d)"
    rf"|\b{_LONG_PIN_LABEL}\s+\d{{3,8}}(?!\d)(?!\s+of\b)"
    rf"|\bpin(?:{_PIN_VALUE_AFTER}| code(?:{_PIN_VALUE_AFTER}|\s+))\d{{3,5}}(?!\d)",
    re.ASCII,
)
# An IBAN: country code, two check digits, then the BBAN in groups of four (spaced or
# not) with a short last group; 15-34 characters once the spaces are gone. One group more
# than the longest IBAN needs leaves room for an IBAN-shaped token before it.
_IBAN = re.compile(r"\b[A-Z]{2}\d{2}(?: ?[A-Z0-9]{4}){2,8}(?: ?[A-Z0-9]{1,3})?\b", re.ASCII)


def _luhn_ok(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = int(ch)
        if i % 2 == 1:
            d = d * 2 - 9 if d > 4 else d * 2
        total += d
    return total % 10 == 0


def _is_card(digits: str) -> bool:
    return any(
        digits.startswith(prefixes) and len(digits) in lengths
        for prefixes, lengths in _CARD_NETWORKS
    ) and _luhn_ok(digits)


def _has_card(text: str) -> bool:
    """Whether a digit run holds a card number, ungrouped or in a printed grouping."""
    for m in _DIGIT_GROUPS.finditer(text):
        groups = re.split(r"[ -]", m.group())
        if any(len(group) in (15, 16) and _is_card(group) for group in groups):
            return True
        for i in range(4):  # a grouped card starts within the first four groups
            for grouping in _CARD_GROUPINGS:
                end = i + len(grouping)
                after = len(groups) - end
                if after < 0 or after > 2 or i + after > 3:
                    continue
                window = groups[i:end]
                if tuple(map(len, window)) != grouping:
                    continue
                around = groups[:i] + groups[end:]
                if all(len(g) <= 4 for g in around) and _is_card("".join(window)):
                    return True
    return False


def _is_iban(candidate: str) -> bool:
    """ISO 13616: a country code and numeric check digits; move the first four characters
    to the end, letters become 10-35, and the number is 1 mod 97."""
    if not (15 <= len(candidate) <= 34 and candidate[:2].isalpha() and candidate[2:4].isdigit()):
        return False
    moved = candidate[4:] + candidate[:4]
    return int("".join(str(int(ch, 36)) for ch in moved)) % 97 == 1


def _has_iban(text: str) -> bool:
    """An IBAN in `text`. A spaced match can take in an uppercase word before or after it
    ("XY12 GB82 ...", "... 1332 EUR"), so each run of its groups is checked (at most ten
    groups, so at most 55 runs)."""
    for m in _IBAN.finditer(text):
        groups = m.group().split(" ")
        for start in range(len(groups)):
            for end in range(len(groups), start, -1):
                if _is_iban("".join(groups[start:end])):
                    return True
    return False


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
    if any(pat.search(text) for pat in (*_HIGH_PATTERNS, _SSN, _STATED_PIN)) or _has_card(text):
        return HIGH
    if any(pat.search(text) for pat in _MEDIUM_PATTERNS) or _has_iban(text):
        return MEDIUM
    return ""


# A line no pattern matches across: `;` ends the assignment shape's token class, and the
# phone shape allows only one space, dot or dash between its groups.
_SCALAR_SEP = "\n;\n"

# A key that names a payment or identity secret, as its last word (`pin`, `badge_pin`,
# `cardNumber`, `security-code`); `pin_count` and `spin` do not.
_PIN_KEYS = {"pin", "passcode", "cvv", "cvc", "cvv2", "cvc2", "securitycode"}
_CARD_KEYS = {"cardnumber", "pan"}
_KEY_WORDS = re.compile(r"[A-Z]+(?![a-z])|[A-Z]?[a-z0-9]+")  # `PIN`, `badge`, `Pin`, `2`


def _keyed_secret(key: str, value: Any) -> str:
    """The stated form of a secret held under a key that names it ("passcode: 4471" for
    `{"badge_pin": "4471"}`), for the detector to read; '' when the key names none or the
    value lacks the secret's shape."""
    if not isinstance(value, str | int) or isinstance(value, bool):
        return ""
    words = [w.lower() for w in _KEY_WORDS.findall(key)]
    last = words[-1:] + ["".join(words[-2:])]
    v = str(value).strip()
    if _PIN_KEYS.intersection(last) and re.fullmatch(r"[0-9]{3,8}", v):
        return f"passcode: {v}"
    if "ssn" in last and re.fullmatch(r"[0-9]{3}-?[0-9]{2}-?[0-9]{4}", v):
        digits = v.replace("-", "")
        return f"{digits[:3]}-{digits[3:5]}-{digits[5:]}"
    if _CARD_KEYS.intersection(last) and _has_card(v):
        return v
    return ""


def detector_text(value: Any) -> str:
    """Decoded text of a stored value for the detector: a JSON string is parsed, and each key
    and scalar is its own line, so a token after a newline keeps its word boundary. The lines
    are joined by `_SCALAR_SEP`, which no pattern crosses, so two scalars never read as one
    (`Password:` then the next key, or `256`, `512`, `1024` as a phone number). A key that
    names a PIN, SSN or card number adds its value's stated form (`_keyed_secret`), so
    `{"pin": "4471"}` reads as `PIN is 4471` does; other keys label nothing. Iterative, so a
    deeply nested value cannot stop a worker pass."""
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
                stated = _keyed_secret(str(k), x)
                if stated:
                    lines.append(stated)
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
# stays a backslash and an `n`). `\uXXXX` is how JSON writes other control characters
# (`\u000b`) even with `ensure_ascii=False`.
_JSON_ESCAPE = re.compile(r'\\(?:(["\\/bfnrt])|u([0-9a-fA-F]{4}))')
_JSON_ESCAPES = {"n": "\n", "t": "\t", "r": "\r", "b": "\b", "f": "\f"}


def _json_unescape(m: re.Match[str]) -> str:
    if m.group(2):
        return chr(int(m.group(2), 16))
    return _JSON_ESCAPES.get(m.group(1), m.group(1))


def reads_high(text: str) -> bool:
    """Whether `text` holds a high shape, whatever the autodetect setting: the rule for
    text copied from a row (a digest's key facts, an observation sent to a model, a raw
    chunk), which has no label of its own or carries one a later sweep may not have set
    yet. Text holding JSON (a chunk of an object value) is read with its string escapes
    undone too, where a token after `\\n` or a quoted password keeps its word boundary."""
    if detect_sensitivity(text) == HIGH:
        return True
    unescaped = _JSON_ESCAPE.sub(_json_unescape, text)
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
    r"who (?:do|should) i (?:call|email|contact)|"
    # an IBAN is labelled medium too: a question for the account is asking for it
    r"ibans?|bank account(?: number)?s?)\b",
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
    """Does the question ask for a way to reach someone, or for a bank account? Then a
    labelled contact detail or IBAN is the answer, not collateral, and the ranking must not
    demote it."""
    return bool(_CONTACT_INTENT.search(query or ""))
