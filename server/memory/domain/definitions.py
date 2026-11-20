"""A definition's names: its `term` and `aliases`, compared in their folded form. Pure.

A name is folded by dropping format characters (Unicode category Cf, such as a pasted
byte-order mark), then `canonical_document_text` (NFKC, trimmed, every run of whitespace
made one space), then `str.casefold` and NFKC once more (folding can leave a sequence
that composes, as in Greek), so `SAT`, `Sat` and `sat ` are one name.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from typing import Any

from server.core.hashing import query_text

DEFINITION_KEY_PREFIX = "definition/"
DEFINITION_DRAFT_KINDS = ("knowledge_gap", "query_history")


def fold_name(name: str) -> str:
    """`name` in the form two names are compared in."""
    return unicodedata.normalize("NFKC", query_text(name).casefold())


def definition_names(value: Any) -> set[str] | None:
    """The folded term and aliases of a decoded definition value, or None when it is not
    one: no object, no non-empty string `term`, or `aliases` present and not a list of
    strings (it may be left out or null)."""
    if not isinstance(value, dict) or not isinstance(value.get("term"), str):
        return None
    aliases = value.get("aliases")
    if aliases is None:
        aliases = []
    if not isinstance(aliases, list) or not all(isinstance(a, str) for a in aliases):
        return None
    return names_of(value["term"], aliases) or None


def written_names(value: Any) -> list[str]:
    """A definition value's term, then its aliases, as written; empty when it holds no
    names."""
    if definition_names(value) is None:
        return []
    return [value["term"], *(value.get("aliases") or [])]


def names_of(term: str, aliases: list[str]) -> set[str]:
    """The folded term and aliases, an empty set when the term folds to nothing."""
    folded = fold_name(term)
    if not folded:
        return set()
    return ({folded} | {fold_name(a) for a in aliases}) - {""}


# ---- where a query names a term (the term leg,
# docs/concepts/identities-and-namespaces.md#governed-definitions-in-search)

# Scripts written without spaces between words: nothing continues a term edge in them.
_UNSPACED = (
    (0x0E00, 0x0EFF),  # Thai, Lao
    (0x1000, 0x109F),  # Myanmar
    (0x1780, 0x17FF),  # Khmer
    (0x3040, 0x30FF),  # Hiragana, Katakana
    (0x31F0, 0x31FF),  # Katakana extensions
    (0x3400, 0x4DBF),  # Han
    (0x4E00, 0x9FFF),
    (0xF900, 0xFAFF),
    (0x20000, 0x323AF),
)
_HANGUL = ((0x1100, 0x11FF), (0x3130, 0x318F), (0xAC00, 0xD7AF))


def _in(c: str, ranges: tuple[tuple[int, int], ...]) -> bool:
    return any(lo <= ord(c) <= hi for lo, hi in ranges)


def _word_char(c: str) -> bool:
    return unicodedata.category(c)[0] in "LN"


def _latin_like(c: str) -> bool:
    """A Latin letter (accented ones included), a decimal digit, `_`, `+` or `#`."""
    if c in "_+#" or c.isdecimal():
        return True
    return c.isalpha() and unicodedata.name(c, "").startswith("LATIN")


def _edge_kind(c: str) -> str:
    if _latin_like(c):
        return "latin"
    if _in(c, _HANGUL):
        return "hangul"
    if _in(c, _UNSPACED) or not c.isalpha():
        return "none"
    return "spaced"


def _continues(edge: str, neighbour: str, *, before: bool) -> bool:
    """Whether `neighbour`, the base of the character next to a term's edge `edge`,
    continues the term (`before`: it comes before the term)."""
    kind = _edge_kind(edge)
    if kind == "latin":
        return _latin_like(neighbour)
    if kind == "hangul":
        return before and _in(neighbour, _HANGUL)
    if kind == "spaced":
        return neighbour.isdecimal() or (neighbour.isalpha() and not _latin_like(neighbour))
    return False


def joined_chars(text: str) -> list[str]:
    """`text` split into characters, each combining mark (category M) joined to the letter
    or number before it; a mark with nothing to join stands alone."""
    out: list[str] = []
    for c in text:
        if unicodedata.category(c)[0] == "M" and out and _word_char(out[-1][0]):
            out[-1] += c
        else:
            out.append(c)
    return out


def _exact_case(word: list[str]) -> bool:
    """A word written all in capitals, or with a capital after its first letter."""
    letters = [c[0] for c in word if c[0].isalpha()]
    return bool(letters) and (
        all(c.isupper() for c in letters) or any(c.isupper() for c in letters[1:])
    )


def _name_pattern(name: list[str]) -> list[tuple[str, bool]]:
    """Each joined character of a name with whether it is compared exactly (it is in a
    word `_exact_case` holds for) or case-folded."""
    exact = [False] * len(name)
    i = 0
    while i < len(name):
        if not _word_char(name[i][0]):
            i += 1
            continue
        end = i
        while end < len(name) and _word_char(name[end][0]):
            end += 1
        if _exact_case(name[i:end]):
            exact[i:end] = [True] * (end - i)
        i = end
    return list(zip(name, exact, strict=True))


@dataclass(frozen=True)
class Name:
    """A term or alias prepared for matching: its joined characters, each with whether
    it is compared exactly, and its case-folded text for a quick substring check."""

    chars: tuple[str, ...]
    exact: tuple[bool, ...]
    folded: str

    @classmethod
    def of(cls, name: str) -> Name:
        chars = joined_chars(query_text(name))
        pattern = _name_pattern(chars)
        return cls(tuple(chars), tuple(e for _, e in pattern), "".join(chars).casefold())


def name_start(query: list[str], name: Name, folded_query: str) -> int | None:
    """Where `name` first appears in `query` (the `joined_chars` of a query's
    `query_text`, whose case-folded text is `folded_query`) with no character next to
    it that continues its edge, or None. A fold that changes a character's length (`ß`
    to `ss`) matches only as written."""
    chars = name.chars
    if not chars or name.folded not in folded_query:
        return None
    first, last = chars[0][0], chars[-1][0]
    for start in range(len(query) - len(chars) + 1):
        if not all(
            q == c if exact else q.casefold() == c.casefold()
            for q, c, exact in zip(
                query[start : start + len(chars)], chars, name.exact, strict=True
            )
        ):
            continue
        end = start + len(chars)
        if start > 0 and _continues(first, query[start - 1][0], before=True):
            continue
        if end < len(query) and _continues(last, query[end][0], before=False):
            continue
        return start
    return None
