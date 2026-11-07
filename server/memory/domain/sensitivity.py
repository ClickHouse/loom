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

The detector is a deterministic lower bound: it reads the label forms and shapes that
docs/architecture/security.md lists, not every way a person can phrase a secret. The
catch-all for extraction is an operator's extraction policy, not this module.
"""

from __future__ import annotations

import bisect
import json
import re
import string
from collections.abc import Callable, Hashable, Iterable, Iterator, Sequence
from typing import Any, Final, Literal, TypeGuard, get_args

from server.memory.domain import chunk_shape

HIGH: Final = "high"
MEDIUM: Final = "medium"
# A row's sensitivity tier, as a write names it.
Sensitivity = Literal["", "low", "medium", "high"]
LEVELS: Final[tuple[Sensitivity, ...]] = get_args(Sensitivity)


def is_level(level: str) -> TypeGuard[Sensitivity]:
    return level in LEVELS


# A block without its closing line runs through its base64 body, so every chunk it covers
# is held. The header-line alternatives come first, or the class would take the `Proc` of
# `Proc-Type:`; the closing-line search stops at the next header to stay linear.
_PRIVATE_KEY_BLOCK = re.compile(
    r"-----BEGIN ((?:RSA |EC |OPENSSH |PGP )?)PRIVATE KEY-----"
    r"(?:(?:(?!-----BEGIN ).)*?-----END \1PRIVATE KEY-----"
    r"|(?:Proc-Type:[^\n]*|DEK-Info:[^\n]*|[A-Za-z0-9+/=\r\n])*)",
    re.S,
)
# The keys a `password=…` style assignment starts with, as words. The sweep's prefilter is
# built from the same tuples.
ASSIGNMENT_KEYS: tuple[tuple[str, ...], ...] = (
    ("password",),
    ("passwd",),
    ("pwd",),
    ("api", "key"),
    ("secret", "key"),
    ("secret",),
    ("access", "token"),
    ("auth", "token"),
    ("bearer",),
    ("client", "secret"),
    ("private", "key"),
)
ASSIGNMENT_WORDS = "|".join("[_-]?".join(words) for words in ASSIGNMENT_KEYS)

# besides whitespace, the characters that end an assignment's value; `_keyed_credential`
# replaces them in what it states
_ASSIGNMENT_STOP_CHARS = "\"',;"
_ASSIGNMENT_VALUE_CHAR = rf"[^\s{re.escape(_ASSIGNMENT_STOP_CHARS)}]"
_ASSIGNMENT_STOPS = str.maketrans(_ASSIGNMENT_STOP_CHARS, "_" * len(_ASSIGNMENT_STOP_CHARS))

# A JWT: header, payload and signature. It starts where a run of its characters starts:
# from `\b`, every `eyJ` after a `-` inside one long run was a start that scanned to the
# run's end (quadratic).
_JWT = re.compile(
    r"(?<![A-Za-z0-9_-])eyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"
)
# What a window of a long value reads as a JWT: a header of at most 1,000 characters and
# the start of its payload, so a long token a window's edge cuts is found from its first
# 1,017 characters (`SHAPE_SPAN`).
_JWT_START = re.compile(r"(?<![A-Za-z0-9_-])eyJ[A-Za-z0-9_-]{10,1000}\.eyJ[A-Za-z0-9_-]{10}")

_HIGH_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bsk-(?:proj-|ant-|lf-)?[A-Za-z0-9_-]{20,}"),  # OpenAI / Anthropic / Langfuse
    re.compile(r"\bpk-lf-[0-9a-fA-F-]{16,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),  # AWS access key id
    re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{30,}\b"),  # GitHub tokens
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{40,}\b"),
    re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"),  # Slack
    _PRIVATE_KEY_BLOCK,
    _JWT,
    re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"),  # Google API key
    # `password=…`, `api_key: …`, `secret := …`, `access_token = …` followed by a token-shaped
    # value of 8+ characters; a space, quote, comma or semicolon ends it.
    re.compile(rf"(?i)\b(?:{ASSIGNMENT_WORDS})\b\s*[:=]\s*[\"']?{_ASSIGNMENT_VALUE_CHAR}{{8,}}"),
)

# An email is `<local>@<domain>`, its parts bounded by RFC 5321's lengths: a local part of
# 1-64 of these characters that starts at a word boundary, a domain of at most 253 with a
# 2-24 letter top level. It is found from its `@`: a regex starting at every word boundary
# rescans up to 64 characters from each one, and its domain again from each start.
_EMAIL_LOCAL_CHARS = string.ascii_letters + string.digits + "._%+-"
_EMAIL_DOMAIN = re.compile(r"@[A-Za-z0-9.-]{1,253}\.[A-Za-z]{2,24}\b")
_WORD_BOUNDARY = re.compile(r"\b")


def has_email(text: str) -> bool:
    """Whether `text` holds an email address; reads each `@` with a domain once."""
    for m in _EMAIL_DOMAIN.finditer(text):
        at = m.start()
        before = text[max(0, at - 64) : at]
        local = len(before) - len(before.rstrip(_EMAIL_LOCAL_CHARS))
        if local:
            start = _WORD_BOUNDARY.search(text, at - local, at)
            if start is not None and start.start() < at:
                return True
    return False


# Phone numbers: E.164 (+14155550123) or grouped national forms with at least 10 digits —
# separators allowed, so "415-555-0123" and "(415) 555 0123" match but a 4-digit year or a
# 6-digit count does not.
PHONE = re.compile(r"(?<![\w.])(?:\+?\d{1,3}[\s.-]?)?\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4}(?![\w.])")

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
# A PIN, passcode, CVV/CVC or security code stated with its value (the forms are listed in
# docs/architecture/security.md). The label strings are plain regex that RE2 reads too: the
# sweep's prefilter is built from them.
#
# `pin` and `pin code`, whose 6-digit values are postal codes ("PIN: 560001"): they take
# 3-5 digits. `pin code` is the one of them that takes its value directly.
PIN_CODE_LABEL = r"pin[_ ]code"
POSTCODE_LIKE_PIN_LABEL = rf"(?:{PIN_CODE_LABEL}|pin)"
# Every other label: 3-8 digits.
PIN_LABEL = r"(?:pin[_ ]number|pass[_ ]?code|cv[vc]2?(?:[_ ](?:code|number))?|security[_ ]code)"
# A label starts after anything but a letter or digit, so a prefix (`atm_pin`) counts and
# a word that ends in one (`spin`) does not.
_LABEL_START = r"(?<![A-Za-z0-9])"


def pin_grammar(space: str) -> tuple[str, str]:
    """The `for ...` qualifier and the `is`/`:` separator before a stated PIN's value, over
    the whitespace class `space`, so the detector and the sweep's RE2 prefilter share one
    grammar. Each whitespace run has its own anchor, so a long run is read once."""
    qualifier = rf"(?:{space}+for(?:{space}+[A-Za-z]+){{1,3}})?"
    separator = rf"(?:{space}+(?:is|was)(?:{space}*[:=])?|{space}*[:=]){space}*"
    return qualifier, separator


_PIN_FOR, _PIN_VALUE_AFTER = pin_grammar(r"\s")
# A value ends where its number does, so a version, a date or a run-on word (`pin is
# 2024.1`, `2024/01/15`, `1234abc`) is not read as one.
_PIN_END = r"(?![-./]?\d)(?![A-Za-z])"
_STATED_PIN = re.compile(
    rf"(?i){_LABEL_START}{PIN_LABEL}{_PIN_FOR}{_PIN_VALUE_AFTER}\d{{3,8}}{_PIN_END}"
    rf"|{_LABEL_START}{PIN_LABEL}\s+\d{{3,8}}{_PIN_END}(?!\s+of\b)"
    rf"|{_LABEL_START}{POSTCODE_LIKE_PIN_LABEL}{_PIN_FOR}{_PIN_VALUE_AFTER}\d{{3,5}}{_PIN_END}"
    rf"|{_LABEL_START}{PIN_CODE_LABEL}\s+\d{{3,5}}{_PIN_END}(?!\s+of\b)",
    re.ASCII,
)
# An IBAN: country code, two check digits, then the BBAN in groups of four (spaced or
# not) with a short last group; 15-34 characters once the spaces are gone. One group more
# than the longest IBAN needs leaves room for an IBAN-shaped token before it.
_IBAN = re.compile(r"\b[A-Z]{2}\d{2}(?: ?[A-Z0-9]{4}){2,8}(?: ?[A-Z0-9]{1,3})?\b", re.ASCII)
# Every regex shape that reads as high; a card number is checked apart (`_has_card`).
_HIGH_SHAPES = (*_HIGH_PATTERNS, _SSN, _STATED_PIN)


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
    """Whether `text` holds a card number, ungrouped or in a printed grouping. The digits
    of an IBAN are left out: its printed groups can read as a card grouping that passes
    Luhn ("DE24 4005 0150 0000 0005 00"), and an IBAN is medium."""
    return bool(_card_spans(text))


def _card_spans(text: str) -> list[tuple[int, int]]:
    """The (start, end) of each run of digit groups in `text` that holds a card number, by
    the rule `_has_card` reads."""
    parts, prev = [], 0
    for start, end in _iban_spans(text):
        parts += [text[prev:start], "x" * (end - start)]
        prev = end
    text = "".join(parts) + text[prev:]
    return [m.span() for m in _DIGIT_GROUPS.finditer(text) if _groups_hold_card(m.group())]


def _groups_hold_card(run: str) -> bool:
    groups = re.split(r"[ -]", run)
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


# The IBAN length of each country in the ISO 13616 registry.
_IBAN_LENGTHS = {
    "AD": 24, "AE": 23, "AL": 28, "AT": 20, "AZ": 28, "BA": 20, "BE": 16, "BG": 22,
    "BH": 22, "BI": 27, "BR": 29, "BY": 28, "CH": 21, "CR": 22, "CY": 28, "CZ": 24,
    "DE": 22, "DJ": 27, "DK": 18, "DO": 28, "EE": 20, "EG": 29, "ES": 24, "FI": 18,
    "FK": 18, "FO": 18, "FR": 27, "GB": 22, "GE": 22, "GI": 23, "GL": 18, "GR": 27,
    "GT": 28, "HN": 28, "HR": 21, "HU": 28, "IE": 22, "IL": 23, "IQ": 23, "IS": 26,
    "IT": 27, "JO": 30, "KW": 30, "KZ": 20, "LB": 28, "LC": 32, "LI": 21, "LT": 20,
    "LU": 20, "LV": 21, "LY": 25, "MC": 27, "MD": 24, "ME": 22, "MK": 19, "MN": 20,
    "MR": 27, "MT": 31, "MU": 30, "NI": 28, "NL": 18, "NO": 15, "OM": 23, "PK": 24,
    "PL": 28, "PS": 29, "PT": 25, "QA": 29, "RO": 24, "RS": 22, "RU": 33, "SA": 24,
    "SC": 31, "SD": 18, "SE": 24, "SI": 19, "SK": 24, "SM": 27, "SO": 23, "ST": 25,
    "SV": 28, "TL": 23, "TN": 24, "TR": 26, "UA": 29, "VA": 22, "VG": 24, "XK": 20,
    "YE": 30,
}  # fmt: skip


def _is_iban(candidate: str) -> bool:
    """ISO 13616: a registered country code, that country's length and numeric check
    digits; move the first four characters to the end, letters become 10-35, and the number
    is 1 mod 97."""
    if len(candidate) != _IBAN_LENGTHS.get(candidate[:2]) or not candidate[2:4].isdigit():
        return False
    moved = candidate[4:] + candidate[:4]
    return int("".join(str(int(ch, 36)) for ch in moved)) % 97 == 1


def _iban_spans(text: str) -> list[tuple[int, int]]:
    """The (start, end) of each IBAN in `text`. A spaced match can take in an uppercase
    word before or after it ("XY12 GB82 ...", "... 1332 EUR"), so each run of its groups is
    checked, longest first (at most ten groups, so at most 55 runs)."""
    spans = []
    for m in _IBAN.finditer(text):
        groups = m.group().split(" ")
        span = None
        for start in range(len(groups)):
            for end in range(len(groups), start, -1):
                if _is_iban("".join(groups[start:end])):
                    offset = m.start() + len(" ".join(groups[:start])) + (start > 0)
                    span = (offset, offset + len(" ".join(groups[start:end])))
                    break
            if span:
                spans.append(span)
                break
    return spans


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


def _reads_high_text(text: str) -> bool:
    """Whether `text` holds a high shape: a credential, a card number, an SSN or a stated PIN."""
    return any(pat.search(text) for pat in _HIGH_SHAPES) or _has_card(text)


def detect_sensitivity(text: str) -> Sensitivity:
    """Classify `text`: 'high' for a credential, card, SSN or stated PIN, 'medium' for
    contact details or an IBAN, '' otherwise. Order matters — a line with both is 'high'."""
    if not text:
        return ""
    if _reads_high_text(text):
        return HIGH
    if has_email(text) or PHONE.search(text) or _iban_spans(text):
        return MEDIUM
    return ""


def detect_window_sensitivity(window: str) -> Sensitivity:
    """`detect_sensitivity` of one window of a long value (`detection_windows`): a JWT
    there is found from its start, since the window's edge may cut its signature off."""
    return HIGH if _JWT_START.search(window) else detect_sensitivity(window)


# A line no pattern matches across: `;` ends the assignment shape's token class, and the
# phone shape allows only one space, dot or dash between its groups.
SCALAR_SEP = "\n;\n"

# Key words naming a PIN-like secret, and the label a value under them is stated with
# (`{"pin_code": "4471"}` reads as `pin is 4471`), so the free-text grammar decides the
# value's length. The sweep's prefilter is built from the same list.
_KEYED_SECRET_PARTS: dict[tuple[str, ...], str] = {
    ("pin",): "pin",
    ("pin", "code"): "pin",
    ("pin", "number"): "pin number",
    ("pass", "code"): "passcode",
    ("cvv",): "cvv",
    ("cvv", "2"): "cvv",
    ("cvv", "code"): "cvv",
    ("cvv", "number"): "cvv",
    ("cvc",): "cvc",
    ("cvc", "2"): "cvc",
    ("cvc", "code"): "cvc",
    ("cvc", "number"): "cvc",
    ("security", "code"): "security code",
}
KEYED_SECRET_WORDS = _KEYED_SECRET_PARTS | {
    ("".join(w),): label for w, label in _KEYED_SECRET_PARTS.items() if len(w) > 1
}
# The key words that state an SSN, written apart or as one word. The spelled-out
# `social_security_number` is read as its last three words.
_SSN_KEY_PARTS = (("ssn",), ("ssn", "number"), ("ssn", "no"), ("social", "security", "number"))
SSN_KEYS = _SSN_KEY_PARTS + tuple(("".join(w),) for w in _SSN_KEY_PARTS if len(w) > 1)
_KEY_WORDS = re.compile(r"[A-Z]+(?![a-z])|[A-Z]?[a-z0-9]+")  # `PIN`, `badge`, `Pin`, `2`
# `ATMPin` and `PINcode` have the same shape, so one split cannot read both: the split
# above reads `ATM`, `Pin`, this one `PIN`, `code`. Both start a word at a single capital,
# so `Spin` is one word.
_KEY_CAPS_WORDS = re.compile(r"[A-Z]{2,}|[A-Z]?[a-z0-9]+")


_ASSIGNMENT_KEY = re.compile(rf"(?i)(?:{ASSIGNMENT_WORDS})")
# A value that names where a secret lives rather than holding it: `env:OPENAI_API_KEY`,
# `vault:kv/db`, `${DB_PASSWORD}`, `$DB_PASSWORD`, a path. A URL is not one: a webhook or a
# presigned URL is the secret itself.
_SECRET_REFERENCE = re.compile(
    # a path: ~/, ./, ../ or two /-segments (a base64 secret may open with one /)
    r"(?i)(?:env|vault|secret|ref):|\$\{|(?-i:\$[A-Z_][A-Z0-9_]*$)|~/|/[^/]+/|\.\.?/"
)


def _keyed_credential(key: str, value: str) -> str:
    """`password: <value>` for a token-shaped string under a key that ends in a credential
    word (`password`, `dbPassword`, `client_secret`), for the assignment pattern to read; ''
    otherwise. The value holds a letter and a digit, so a form field (`"required"`), a mask
    (`"********"`) or a placeholder (`"YOUR_API_KEY"`, `"<redacted>"`) is not a credential."""
    words = [w.lower() for w in _KEY_WORDS.findall(key)]
    if words == ["pwd"]:
        return ""  # an environment's working directory, not a password
    if not any(_ASSIGNMENT_KEY.fullmatch("_".join(words[-n:])) for n in (1, 2) if len(words) >= n):
        return ""
    v = value.strip()
    if len(v) < 8 or any(c.isspace() for c in v) or _SECRET_REFERENCE.match(v):
        return ""
    if not (any(c.isalpha() for c in v) and any(c.isdigit() for c in v)):
        return ""
    return "password: " + v.translate(_ASSIGNMENT_STOPS)


def _keyed_secret(key: str, value: Any) -> str:
    """The stated form of a secret held under a key that names it ("pin is 4471" for
    `{"badge_pin": "4471"}`, "cvv is 123" for `{"cvv_2": "123"}`, "password: hunter2xyz" for
    `{"db_password": "hunter2xyz"}`), for the detector to read; '' when the key names none."""
    if not isinstance(value, str | int) or isinstance(value, bool):
        return ""
    if isinstance(value, str) and (credential := _keyed_credential(key, value)):
        return credential
    # the last one, two and three words of both splits, lowercased
    tails = [
        tuple(w.lower() for w in words[-n:])
        for words in (_KEY_WORDS.findall(key), _KEY_CAPS_WORDS.findall(key))
        for n in (1, 2, 3)
        if len(words) >= n
    ]
    label = next((KEYED_SECRET_WORDS[t] for t in tails if t in KEYED_SECRET_WORDS), "")
    if not (label or any(t in SSN_KEYS for t in tails)):
        return ""
    v = str(value).strip(" \t\n\r\f\v")  # the whitespace the patterns read, ASCII only
    if label:
        return f"{label} is {v}" if v.isdigit() else ""
    if re.fullmatch(r"[0-9]{3}-?[0-9]{2}-?[0-9]{4}", v):
        digits = v.replace("-", "")
        return f"{digits[:3]}-{digits[3:5]}-{digits[5:]}"
    return ""


# A `"key": value` pair in raw JSON text, for `reads_high`. A quoted value must be the
# whole string, one token of at most 1024 characters (a longer one is read decoded only,
# by `detector_text`), and a bare number must end at its digits; a key reads at most 200
# characters, so the scan stays linear.
_KEYED_PAIR = re.compile(
    r'"((?:[^"\\]|\\.){1,200})"\s*:\s*'
    r'(?:"\s*((?:[^"\\\s]|\\.){1,1024})\s*"|([0-9][0-9-]{0,15})(?![\w.-]))',
    re.ASCII,
)


def _pair_secret(key: str, quoted: str | None, bare: str | None) -> str:
    """`_keyed_secret` of a raw pair, its quoted value decoded as `detector_text` reads it."""
    return _keyed_secret(key, _JSON_ESCAPE.sub(_json_unescape, quoted) if quoted else bare or "")


def detector_text(value: Any) -> str:
    """Decoded text of a stored value for the detector: a JSON string is parsed, and each key
    and scalar is its own line, so a token after a newline keeps its word boundary. The lines
    are joined by `SCALAR_SEP`, which no pattern crosses, so two scalars never read as one
    (`Password:` then the next key, or `256`, `512`, `1024` as a phone number). A key that
    names a PIN-like secret, an SSN or a credential adds its value's stated form
    (`_keyed_secret`), so `{"pin": "4471"}` reads as `pin is 4471` does; other keys label
    nothing. Iterative, so a deeply nested value cannot stop a worker pass."""
    return SCALAR_SEP.join(detector_lines(value))


def detector_lines(value: Any) -> Iterator[str]:
    """The lines `detector_text` joins, one at a time, so a caller can pause between them."""
    if isinstance(value, str):
        text = value
        try:
            value = json.loads(text)
        except (ValueError, RecursionError):
            yield text
            return
    stack: list[Any] = [value]
    while stack:
        v = stack.pop()
        if isinstance(v, dict):
            for k, x in reversed(list(v.items())):
                stack.append(x)
                stack.append(str(k))
                stated = _keyed_secret(str(k), x)
                if stated:
                    yield stated
        elif isinstance(v, list | tuple):
            stack.extend(reversed(v))
        elif v is not None:
            yield str(v)


def value_reads_high(value: Any) -> bool:
    """Whether a stored value holds a high shape (a credential, card number, SSN or stated
    PIN), read decoded (`detector_text`), whatever the autodetect setting. For a value; raw
    stored text (a chunk, a passage) goes through `reads_high`, which also undoes JSON
    escapes."""
    return _reads_high_text(detector_text(value))


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
    undone too, where a token after `\\n` or a quoted password keeps its word boundary, and
    its `"key": value` pairs are read as `detector_text` reads them (`{"badge_pin": "4471"}`,
    `{"password": "hunter2xyz"}`)."""
    texts = {text, _JSON_ESCAPE.sub(_json_unescape, text)}
    if any(_reads_high_text(t) for t in texts):
        return True
    return any(
        _reads_high_text(_pair_secret(k, quoted, bare))
        for t in texts
        for k, quoted, bare in _KEYED_PAIR.findall(t)
    )


def _high_text_spans(text: str) -> list[tuple[int, int]]:
    """The [start, end) ranges of `text` that `_reads_high_text` reads as high, with each
    `"key": value` pair that states a secret (`{"badge_pin": "4471"}`)."""
    spans = [m.span() for pat in _HIGH_SHAPES for m in pat.finditer(text)]
    spans += _card_spans(text)
    spans += [
        m.span()
        for m in _KEYED_PAIR.finditer(text)
        if _reads_high_text(_pair_secret(m.group(1), m.group(2), m.group(3)))
    ]
    return spans


def high_spans(text: str) -> list[tuple[int, int]]:
    """The [start, end) character ranges of `text` that read as high, by the rule
    `reads_high` applies: as written, and with JSON string escapes undone."""
    spans = _high_text_spans(text)
    escapes = list(_JSON_ESCAPE.finditer(text))
    if not escapes:
        return spans
    unescaped = _JSON_ESCAPE.sub(_json_unescape, text)
    # Each escape decodes to one character: where that character sits in `unescaped`, and
    # how much shorter the text is once that escape and every one before it are decoded.
    decoded_at: list[int] = []
    shortened: list[int] = []
    removed = 0
    for m in escapes:
        decoded_at.append(m.start() - removed)
        removed += len(m.group()) - 1
        shortened.append(removed)

    def in_text(pos: int) -> int:
        before = bisect.bisect_left(decoded_at, pos)
        return pos + (shortened[before - 1] if before else 0)

    spans += [(in_text(start), in_text(end)) for start, end in _high_text_spans(unescaped)]
    return spans


def overlaps_high(source: str, ranges: list[tuple[int, int]]) -> list[bool]:
    """For each [start, end) range of `source` (a chunk's place in the text it was cut from),
    whether it overlaps a high shape in `source` or its own text reads as high. A chunk cut
    through a key holds only its head or its tail, which does not read as high on its
    own; a chunk that starts inside a word can read as high where `source` does not."""
    spans = high_spans(source)
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


def _joined_runs(texts: dict[int, str]) -> Iterator[tuple[str, list[int], list[tuple[int, int]]]]:
    """Each run of a session's chunks at consecutive indexes, `texts` keyed by `chunk_index`:
    the run's text with each chunk's overlap with the one before dropped, its indexes, and
    each chunk's [start, end) range in that text. A gap in `chunk_index` starts a new run."""
    runs: list[list[int]] = []
    for index in sorted(texts):
        if runs and index == runs[-1][-1] + 1:
            runs[-1].append(index)
        else:
            runs.append([index])
    for indexes in runs:
        joined = ""
        ranges: list[tuple[int, int]] = []
        for index in indexes:
            content = texts[index]
            repeat = _overlap(joined, content)
            ranges.append((len(joined) - repeat, len(joined) + len(content) - repeat))
            joined += content[repeat:]
        yield joined, indexes, ranges


def joined_overlaps_high(chunks: Sequence[tuple[Hashable, int, str]]) -> list[bool]:
    """For each `(session, chunk_index, content)` chunk, whether it overlaps a high shape in
    the text its session's chunks make joined in `chunk_index` order, each chunk's overlap with
    the one before dropped, or its own text reads as high: the rule the other chunk writers
    apply to the text they cut (`overlaps_high`). Chunks with a gap in `chunk_index`
    are not joined. A session that holds two texts at one index (written by two calls) has
    no single text to rebuild, so its chunks are read in pairs instead
    (`_held_in_pairs`)."""
    by_index: dict[Hashable, dict[int, list[int]]] = {}
    for i, (session, index, _) in enumerate(chunks):
        by_index.setdefault(session, {}).setdefault(index, []).append(i)
    held = [False] * len(chunks)
    for positions in by_index.values():
        texts = {index: {chunks[i][2] for i in at} for index, at in positions.items()}
        if any(len(versions) > 1 for versions in texts.values()):
            pairs_held = _held_in_pairs(texts)
            for index, at in positions.items():
                for i in at:
                    held[i] = (index, chunks[i][2]) in pairs_held
            continue
        single = {index: next(iter(versions)) for index, versions in texts.items()}
        for joined, indexes, ranges in _joined_runs(single):
            for index, hit in zip(indexes, overlaps_high(joined, ranges), strict=True):
                for i in positions[index]:
                    held[i] = hit
    return held


def _held_in_pairs(texts: dict[int, set[str]]) -> set[tuple[int, str]]:
    """The `(chunk_index, text)` chunks of a session with several texts at one index that
    read as high on their own, or that overlap a high shape in the text a chunk and a chunk
    at the next index make joined. Only a pair whose texts overlap (`_overlap`) was cut
    from one text, so only such a pair is joined."""
    held = {
        (index, text) for index, versions in texts.items() for text in versions if reads_high(text)
    }
    for index, versions in texts.items():
        for first in versions:
            for second in texts.get(index + 1, set()):
                repeat = _overlap(first, second)
                if not repeat:
                    continue
                joined = first + second[repeat:]
                ranges = [(0, len(first)), (len(first) - repeat, len(joined))]
                first_hit, second_hit = overlaps_high(joined, ranges)
                if first_hit:
                    held.add((index, first))
                if second_hit:
                    held.add((index + 1, second))
    return held


# Spare rows a read asks for, so a passage it holds back for reading as high does
# not leave the page short.
HELD_MARGIN = 2  # not a setting: algorithm shape, sized to how rarely a row is held


def without_high[T](
    items: Iterable[T], text: Callable[[T], str], limit: int | None = None
) -> list[T]:
    """`items`, in order, whose text does not read as high, the first `limit` of
    them. The text is checked only as far as `limit` reaches."""
    kept: list[T] = []
    for item in items:
        if limit is not None and len(kept) >= limit:
            break
        if not reads_high(text(item)):
            kept.append(item)
    return kept


def listed_text(text: Any) -> str:
    """`text` as a namespace-wide listing shows it: every reader of the namespace sees a
    listing, so text that reads as high is shown empty."""
    t = str(text or "")
    return "" if reads_high(t) else t


def resolve_sensitivity(requested: str | None, text: str) -> Sensitivity:
    """The caller's explicit level wins; an unset level is detected from the
    value when `memory_sensitivity_autodetect` is on, else stays ''."""
    level = caller_level(requested)
    return detect_sensitivity(text or "") if level is None else level


def caller_level(requested: str | None) -> Sensitivity | None:
    """The level a write gets without reading its value: the caller's explicit level, or
    '' when `memory_sensitivity_autodetect` is off; None when it is to be detected."""
    level = (requested or "").strip().lower()
    if level and is_level(level):
        return level
    from server.settings import settings

    return None if bool(settings.memory_sensitivity_autodetect) else ""


# The longest text a shape is read over, so a window of a long value holds each shape
# whole: an email is at most 64 + 1 + 253 + 1 + 24 = 343 characters, a JWT in a window
# is found from its first 1,017 (`_JWT_START`), and a key, a private key block, a card,
# an SSN, a phone number or an IBAN from fewer. Three are not: a stated PIN with about
# 1,000 characters of whitespace or qualifier words before its value, a `password=`
# assignment with about 1,000 characters of whitespace around its `=`, and a JWT whose
# header passes 1,000 characters. Across a window's edge, those read lower than the
# whole value.
SHAPE_SPAN = 1024
# Where a window ends and the next begins: after whitespace that no digit or dash touches,
# so no digit run (a card, a phone number, an SSN) is split, and each edge reads as the
# whitespace it stands for. Without one in `_CUT_SEARCH` characters, the cut is where it
# falls, and a shape at that edge can read where the whole value would not.
_WINDOW_CUT = re.compile(r"(?<![\d-])\s(?![\d-])")
_CUT_SEARCH = 256


def _cut(text: str, at: int) -> int:
    """The last window cut at or before `at`, within `_CUT_SEARCH` characters."""
    last = at
    for m in _WINDOW_CUT.finditer(text, max(0, at - _CUT_SEARCH), min(len(text), at + 1)):
        if m.end() <= at:
            last = m.end()
    return last


def detection_windows(text: str, size: int) -> Iterator[str]:
    """`text` in windows of at most `size` characters (at least `4 * SHAPE_SPAN`, so each
    window moves past the last), each overlapping the next by at least `SHAPE_SPAN`, so
    every shape found from a prefix that long lies whole in one window. The largest level
    of the windows is the level of `text`, but for the shapes `SHAPE_SPAN` names, and a
    shape at a cut where no whitespace was near, which may read where the whole would not."""
    if size < 4 * SHAPE_SPAN:
        raise ValueError(f"a window holds at least {4 * SHAPE_SPAN} characters, not {size}")
    start = 0
    while len(text) - start > size:
        end = _cut(text, start + size)
        yield text[start:end]
        start = _cut(text, end - SHAPE_SPAN)
    yield text[start:]


def max_sensitivity(levels: Iterable[str | None]) -> Sensitivity:
    """The highest tier among `levels` ('' < low < medium < high); unknown
    strings count as ''."""
    best = 0
    for lv in levels:
        v = (lv or "").strip().lower()
        if is_level(v):
            best = max(best, LEVELS.index(v))
    return LEVELS[best]


def inherited_sensitivity(source_levels: Iterable[str | None], text: str) -> Sensitivity:
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
