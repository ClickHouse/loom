"""A fact the session's own Loom tools carried is not a new memory.

At SessionEnd the plugin hands `memory.set_from_messages` the session's turns
and `session_values`: every string argument of the session's
`loom_*` tool calls (the value it wrote, the query it asked, the title it
saved) and every result those calls returned. The extractor reads the
assistant's summary of that work and writes it back as facts: the value the
session set a minute earlier ("I prefer squash merges for small PRs." — after
the same session had updated the key to rebase), the runbook it stored, the
note it appended ("step 5b done"), the query it ran ("searched for what do I
like to drink?"), the bookmark text a context pack returned ("the
api-server session had 34 uncommitted files"). Each is either a copy
of a row the tool already persisted or served, or a stale version of one.

A fact is an echo when a session value appears in it (a written value or an
argument quoted inside the fact), or when nearly all of the fact's content
words come from one session value (the fact restates a tool's text).
Deterministic; no model call.
"""

from __future__ import annotations

import json
import re
from typing import Any

# Words that carry no subject: articles, auxiliaries, the actors every
# extracted sentence starts with, and the verbs the extractor wraps a
# restated value in.
_STOP = frozenset(
    """
    a an the and or but of to in on at for from by with as is are was were be been being
    it its this that these those there here than then so if not no yes do does did done
    has have had will would can could should may might must shall into onto about over
    user users user's assistant agent i me my we our you your they their he she his her
    prefers prefer preferred likes like says said noted stated reported uses used
    """.split()
)
_TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9._/:+-]*[a-z0-9]|[a-z0-9]")
# A session value quoted inside a fact counts only when it says something:
# at least this many content words (a bare "semantic" or a namespace name
# appears in half the facts of a namespace).
_QUOTE_MIN_WORDS = 2
# A fact restates a tool's text when this share of its content words (and at
# least this many of them) come from one session value.
_RESTATE_SHARE = 0.8
_RESTATE_MIN_WORDS = 3
# ...and the value is at most this many times the fact's content words.
_RESTATE_MAX_RATIO = 4


def _norm(text: str) -> str:
    return " ".join(_TOKEN_RE.findall(str(text or "").lower()))


_JOINT_RE = re.compile(r"[._/:+-]+")


def _words(text: str) -> set[str]:
    """Content words, plus the parts of a joined identifier: a tool result's
    JSON says `total_memories`, the fact restating it says "memories"."""
    out: set[str] = set()
    for w in _TOKEN_RE.findall(str(text or "").lower()):
        out.add(w)
        out.update(p for p in _JOINT_RE.split(w) if p)
    return out - _STOP


def _fact_text(fact: dict[str, Any]) -> str:
    value = fact.get("value")
    if isinstance(value, dict):
        for k in ("text", "value", "fact", "content"):
            v = value.get(k)
            if isinstance(v, str):
                return v
        return " ".join(str(v) for v in value.values() if isinstance(v, str))
    return str(value or "")


class _Value:
    __slots__ = ("norm", "words", "quotable")

    def __init__(self, text: str) -> None:
        self.norm = _norm(text)
        self.words = _words(text)
        # Counted in WHITESPACE tokens — `_words` also splits a joined
        # identifier, so a bare namespace name like `team-alerts` would be
        # "quotable" and drop every real fact that merely named the namespace.
        self.quotable = (
            len(self.norm.split()) >= _QUOTE_MIN_WORDS and len(self.words) >= _QUOTE_MIN_WORDS
        )


# A one-word argument ("loomcheck", a title, a thread id) counts only when the
# fact QUOTES it — 'loomcheck', "loomcheck" or `loomcheck` — and it is long
# enough to be a name rather than a common word.
_QUOTED_RE = re.compile(r"""['"`\u2018\u201c]([^'"`\u2019\u201d]{6,120})['"`\u2019\u201d]""")

# Loom's own feature-check prompts use disposable keys. A pasted test plan can
# name a preference-shaped value in a user turn without asserting it as the
# user's preference. Only the quoted values AFTER these keys are test data;
# other claims in the same session still go through normal extraction.
_LOOMCHECK_KEY_RE = re.compile(r"(?<![\w/])test/loomcheck(?:-[a-z0-9_-]+)?/[\w./-]+", re.I)


def synthetic_test_values(messages: list[dict[str, Any]] | None) -> list[str]:
    """Quoted values assigned to disposable Loom-check keys in user prompts."""
    values: list[str] = []
    for message in messages or []:
        if str(message.get("role") or "") != "user":
            continue
        for line in str(message.get("content") or "").splitlines():
            for key in _LOOMCHECK_KEY_RE.finditer(line):
                match = _QUOTED_RE.search(line[key.end() : key.end() + 240])
                if match:
                    value = match.group(1).strip()
                    if len(_words(value)) >= 3 and value not in values:
                        values.append(value)
    return values[:80]


# A code identifier in a fact — a backticked span, a dotted or
# underscored name, a path, a CamelCase word. A fact whose identifiers all
# come from one code-tool result, with most of its words, restates that
# result ("`?toolset=qa` maps to `tracker` through `TOOLSET_ALIASES`" after
# loom_code_grep returned the mapping); the index answers it fresh next time.
_BACKTICK_RE = re.compile(r"`([^`\n]{1,120})`")
_IDENT_RE = re.compile(
    r"(?<![\w./-])(?:[A-Za-z_][\w-]*[._/][\w./-]*\w|_[A-Za-z]\w+|[A-Z][a-z]+[A-Z]\w*|"
    r"[A-Z]{2,}_[A-Z_]+)(?![\w/-])"
)
_IDENT_SHARE = 0.6
_IDENT_MIN_WORDS = 4


_EMAIL_RE = re.compile(r"\S+@\S+")


def _identifiers(text: str) -> set[str]:
    text = _EMAIL_RE.sub(" ", text)
    out = {_norm(m) for m in _BACKTICK_RE.findall(text)}
    out.update(_norm(m) for m in _IDENT_RE.findall(_BACKTICK_RE.sub(" ", text)))
    return {i for i in out if i}


def _restates_code_result(text: str, words: set[str], v: _Value) -> bool:
    idents = _identifiers(text)
    # Code-quoted, or two names: one bare dotted token is as often an email
    # or a host ("User's email is …" restated from a search result is the
    # extractor's business, not an echo of code).
    if len(idents) < 2 and not _BACKTICK_RE.search(text):
        return False
    if len(words) < _IDENT_MIN_WORDS:
        return False
    if not all(i in v.norm for i in idents):
        return False
    return len(words & v.words) >= _IDENT_SHARE * len(words)


def is_echo(fact: dict[str, Any], values: list[_Value]) -> bool:
    text = _fact_text(fact)
    norm = _norm(text)
    words = _words(text)
    if not norm or not words:
        return False
    quoted = {_norm(q) for q in _QUOTED_RE.findall(text)}
    for v in values:
        if v.norm and v.norm in quoted:
            return True
        if v.quotable and v.norm and f" {v.norm} " in f" {norm} ":
            return True
        # A restatement is of a value of comparable size. Every
        # word of a short fact ("User's Loom namespace is team-alerts") also occurs
        # somewhere in an 8K search-result blob; a big value counts only through
        # a quoted multi-word span (the checks above).
        if (
            len(words) >= _RESTATE_MIN_WORDS
            and len(v.words) <= _RESTATE_MAX_RATIO * len(words)
            and len(words & v.words) >= _RESTATE_SHARE * len(words)
        ):
            return True
        if _restates_code_result(text, words, v):
            return True
    return False


# A search or get result is one JSON blob holding many rows;
# each row's own text is what a restating fact copies ("User prefers rebase
# merges for small PRs." from a `content_excerpt` inside a 4 KB loom_search
# result). The blob is too big to be restated as a whole (the size guard
# above), so each row text is compared on its own as well.
_ROW_TEXT_FIELDS = frozenset({"value", "content_excerpt", "content", "text", "fact", "summary"})
_KEY_SUFFIX_RE = re.compile(r"\s*\(#[^)]*\)\s*$")
_ROW_TEXTS_MAX = 400


def _row_texts(blob: str) -> list[str]:
    s = blob.strip()
    if not s.startswith(("{", "[")):
        return []
    try:
        doc = json.loads(s)
    except ValueError:
        return []
    out: list[str] = []

    def walk(x: Any, depth: int) -> None:
        if depth > 8 or len(out) >= _ROW_TEXTS_MAX:
            return
        if isinstance(x, dict):
            for k, y in x.items():
                if k in _ROW_TEXT_FIELDS and isinstance(y, str) and len(y.strip()) >= 8:
                    out.append(_KEY_SUFFIX_RE.sub("", y).strip().strip('"'))
                else:
                    walk(y, depth + 1)
        elif isinstance(x, list):
            for y in x:
                walk(y, depth + 1)

    walk(doc, 0)
    return out


def _with_row_texts(session_values: list[str] | None) -> list[str]:
    out: list[str] = []
    for v in session_values or []:
        out.append(v)
        out.extend(_row_texts(str(v or "")))
    return out


def partition(
    facts: list[dict[str, Any]], session_values: list[str]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """(kept, dropped) — dropped restate a session value."""
    values = [_Value(v) for v in _with_row_texts(session_values) if str(v or "").strip()]
    if not values:
        return list(facts), []
    kept: list[dict[str, Any]] = []
    dropped: list[dict[str, Any]] = []
    for f in facts:
        (dropped if is_echo(f, values) else kept).append(f)
    return kept, dropped
