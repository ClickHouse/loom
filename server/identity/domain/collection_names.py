"""The names a collection's title derives, for `collection.create` with
`derive_name`: the plain slug first, then the slug with a short suffix. The
suffixes are a hash of the request, so a retry walks the same names and finds
the collection its first attempt made."""

from __future__ import annotations

import hashlib
import unicodedata
from collections.abc import Iterator

from server.core.naming import slugify
from server.core.principals import COLLECTION_PREFIX, PERSONAL_PREFIX

SLUG_LEN = 40  # not a setting: a retry must derive the same names
# Names tried before the create gives up; each is one round of the taken checks.
CANDIDATES = 16  # not a setting: a retry must derive the same names
_SUFFIX_LEN = 4
_DIGITS = "0123456789abcdefghijklmnopqrstuvwxyz"


def title_key(title: str) -> str:
    """What two titles must share to be the same title: case, runs of whitespace,
    characters that may draw as nothing and how a character is spelled in Unicode do
    not count."""
    text = unicodedata.normalize("NFKC", title)
    # A character Unicode lets a renderer draw as nothing (a soft hyphen, a variation
    # selector): two titles that differ only by one are the same title. The joiners and
    # tag characters stay: they make emoji and spell some scripts, so titles that differ
    # by one can look different (two flags, a family and three people).
    visible = "".join(ch for ch in text if ch in SHAPING or not _ignorable(ord(ch)))
    return " ".join(visible.split()).casefold()


# Unicode's Default_Ignorable_Code_Point (DerivedCoreProperties.txt), as ranges.
_IGNORABLE = (
    (0x00AD, 0x00AD),
    (0x034F, 0x034F),
    (0x061C, 0x061C),
    (0x115F, 0x1160),
    (0x17B4, 0x17B5),
    (0x180B, 0x180F),
    (0x200B, 0x200F),
    (0x202A, 0x202E),
    (0x2060, 0x206F),
    (0x3164, 0x3164),
    (0xFE00, 0xFE0F),
    (0xFEFF, 0xFEFF),
    (0xFFA0, 0xFFA0),
    (0xFFF0, 0xFFF8),
    (0x1BCA0, 0x1BCA3),
    (0x1D173, 0x1D17A),
    (0xE0000, 0xE0FFF),
)


# Ignorable characters that shape what is drawn: the zero-width joiner and non-joiner,
# and the tag characters of subdivision flags.
SHAPING = frozenset("\u200c\u200d") | {chr(c) for c in range(0xE0020, 0xE0080)}


def _ignorable(cp: int) -> bool:
    return any(lo <= cp <= hi for lo, hi in _IGNORABLE)


def name_candidates(org: str, caller: str, parent: str, title: str) -> Iterator[str]:
    """The names `title` derives, in the order they are tried."""
    slug = _slug(title)
    if f"{COLLECTION_PREFIX}{slug}-".startswith(PERSONAL_PREFIX):
        slug = f"c-{slug}"  # col-me-… is kept for personal collections
    yield f"{COLLECTION_PREFIX}{slug}"
    seed = "\0".join((org, caller, parent, title_key(title)))
    for i in range(1, CANDIDATES):
        yield f"{COLLECTION_PREFIX}{slug}-{_suffix(seed, i)}"


def spells(title: str) -> bool:
    """Whether the names `title` derives spell it, rather than hash it: only
    then does an untitled collection's name say it is that title's."""
    return bool(_text_slug(title))


def _text_slug(title: str) -> str:
    # "Café" reads as "cafe". The cut can end on a hyphen.
    # Case folded first (upper then lower, as the dashboard does), so "ß" reads as "ss"
    # and "ı" as "i" rather than dropping out.
    folded = unicodedata.normalize("NFKD", title.upper().lower()).encode("ascii", "ignore").decode()
    return slugify(folded, max_len=SLUG_LEN).rstrip("-")


def _slug(title: str) -> str:
    # A title with no Latin letters or digits (a CJK one, an emoji) slugs to
    # nothing: a hash of it then, so two such titles do not share one name.
    slug = _text_slug(title)
    if slug:
        return slug
    digest = int(hashlib.sha256(title_key(title).encode()).hexdigest(), 16)
    return f"c-{_base36(digest)}"


def _suffix(seed: str, i: int) -> str:
    return _base36(int(hashlib.sha256(f"{seed}\0{i}".encode()).hexdigest(), 16))


def _base36(n: int) -> str:
    out = []
    for _ in range(_SUFFIX_LEN):
        n, d = divmod(n, len(_DIGITS))
        out.append(_DIGITS[d])
    return "".join(out)
