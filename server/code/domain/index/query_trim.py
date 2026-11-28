"""A long question is asked with its identifiers, the words it repeats and its first page.

An issue-shaped question arrives as the issue body: a greeting or a template, the
subject, a reproducer, its output and the links, in no fixed order. The lexical leg
sums idf times presence over at most `LEXICAL_TOKEN_CAP` lexemes, so every lexeme the
subject's code does not hold only adds other chunks to the pool. What marks a subject
lexeme is not where it stands or how rare it is: a word that is rare in code is rare
because it is not code vocabulary, and an opening can be a greeting. A question names
its subject as code, or returns to it.

This module is the plan for such a question, pure and unit-testable:

* `strip_boilerplate` removes the template headings by a fixed list — they
  arrive flattened into one line, so the match is a case-insensitive phrase,
  not a line — and counts what it removed.
* `strip_non_terms` removes what names no subject before the tokenizer splits it:
  URLs, UUIDs and hashes.
* `term_counts` counts how often the question says each lexeme. An English function
  word counts only where the question writes it as code (`only()`, `qs.only`,
  `returns None`): prose repeats it by grammar. The directories of a file path are
  not counted: every frame of a traceback repeats them, and they say where the file
  is installed. The file's own name is counted.
* `select_terms` keeps, for at most `WORD_SHARE` of the list, the words: those the
  question says more than once first, then the others, each in reading order. The
  whole forms of the identifiers (camel / snake / `a::b`) take the rest. When more
  are left than fit, the words it repeats stay, then its identifiers, the rarest
  first, then the words it says once. A caller's terms take the slots the question
  leaves, the whole forms of their identifiers before their other lexemes, the
  rarest first: the question is asked with the same known terms with and without
  them. A word that more than `COMMON_SHARE` of the chunks hold, a function word the
  question never writes as code, a bare number, and a lexeme the corpus does not
  contain (df 0 admits no row and scores nothing) fill a slot, in that order, only
  when nothing better is left.
* the vector leg embeds the first `EMBED_MAX_WORDS` words of the cleaned
  text instead of the raw body.

Known limits: a subject the question states once, as a plain word, after its word
share is already taken is not asked for, nor is one that more than `COMMON_SHARE` of
the chunks hold when other words fill the share. The function words are English, and
one that is also a name in code is asked for as a word only where it is written as
code: `over` in `an aggregate over a window` is not. A capitalised one after a
heading that lost its line break, or in a title in title case, reads as code.
Repetition marks a format as well as a subject: each frame of a Python traceback
repeats `File` and `line`, and these take two word slots; the functions the frames
name are identifiers and are kept. A route written like a path (`/admin/login/`) is
read as one, so its words are not counted as repeated. A question that
fills the list alone is asked without a caller's terms. A short id that reads as a
word and a number (`ff0000`, `a456`) is kept as an identifier, as a code (`f401`) is.

Nothing here runs for a question whose tokenizer output fits the cap: the
plan's `lexical_query`, `embed_text` and `tokens` are then the raw inputs,
so every statement and the embedding cache key are byte-identical to the
untrimmed path (asserted in tests/unit/test_code_search_query_trim.py).
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field

from server.code.domain.extract.tokenize import code_tokens, compound_identifiers

# The issue template's headings, as GitHub renders them into a body. The
# match is a case-insensitive phrase anywhere in the text (a consumer that
# pastes the body flattens its lines), optionally followed by a colon.
BOILERPLATE_HEADINGS: tuple[str, ...] = (
    "Company or project name",
    "Describe what's wrong",
    "Describe the unexpected behaviour",
    "Describe the unexpected behavior",
    "Describe the situation",
    "How to reproduce",
    "Expected behavior",
    "Expected behaviour",
    "Error message and/or stacktrace",
    "Additional context",
    "_No response_",
)

# The lexical leg's list is bounded at 32 lexemes; the trim chooses WHICH, never widens.
LEXICAL_TOKEN_CAP = 32
# Words take at most this share of the list, also when the identifiers leave slots
# free: a word said once is as often prose as subject, and each one adds the chunks
# that hold it to the pool. The identifiers take the rest; more than fit are cut to
# the rarest. Half is a round value: no sweep chose it.
WORD_SHARE = 0.5
# A word that more of the chunks hold than this ranks little and opens the gate to
# that share of the namespace: it is asked for after every other word. One in eight
# is a round value: no sweep chose it.
COMMON_SHARE = 1 / 8
# The vector leg embeds at most this many whitespace words of the cleaned text (~2,000
# characters of prose: the request cap for `query`).
EMBED_MAX_WORDS = 512
# English function words, by class. A question repeats them more than its subject, so
# one counts as said only where the question writes it as code (`written_as_code`).
# The tokenizer splits a contraction at its apostrophe and drops the one-letter half,
# so the other half is listed with them.
_FUNCTION_WORDS = frozenset(
    (
        # determiners
        "a an the this that these those all any both each every either neither some such no "
        "many much more most few several other another enough "
        # pronouns
        "me we us you he him she her it they them my mine our ours your yours his hers its their "
        "theirs myself ourselves yourself yourselves himself herself itself themselves "
        "who whom whose which what none anyone anything anybody everyone everything everybody "
        "someone something somebody nobody nothing "
        # auxiliaries and modals
        "be am is are was were been being have has had having do does did doing "
        "can cannot could may might must shall should will would ought "
        # prepositions
        "about above across after against along among around as at before behind below beneath "
        "beside besides between beyond by despite down during except for from in inside into like "
        "near of off on onto out outside over per since than through throughout to toward towards "
        "under until up upon via with within without "
        # conjunctions
        "and or but nor so yet if then because although though unless while whereas whether "
        # question and place words, negation, focus and degree particles
        "when where why how here there not only also just too very even "
        # halves of contractions
        "don doesn didn isn aren wasn weren hasn haven hadn won wouldn couldn shouldn mustn ain "
        "ve ll re"
    ).split()
)

_BOILERPLATE_RE = re.compile(
    "|".join(
        r"(?<![A-Za-z0-9])" + re.escape(h).replace("'", "['’]") + r"(?![A-Za-z0-9])\s*:?"
        for h in sorted(BOILERPLATE_HEADINGS, key=len, reverse=True)
    ),
    re.IGNORECASE,
)
# A `Namespace::name` reference: both sides are identifiers to keep. Group 1
# is the name, read from a word's first letter so a long word costs one pass.
_QUALIFIED_RE = re.compile(
    r"(?<![A-Za-z0-9_])[0-9]*+([A-Za-z_][A-Za-z0-9_]*+(?:::[A-Za-z_][A-Za-z0-9_]*+)+)"
)
# A piece of an id is four or more hex characters, the shortest group of a UUID: a
# shorter word (`e2e`, `b2b`) is a name.
_HEX_RE = re.compile(r"[0-9a-f]{4,}")
# A commit or fiddle hash is seven or more of them, the shortest form git prints.
HASH_MIN_CHARS = 7
# An address or an id, not a subject: a URL with its host, route words, hash and line
# anchor, and a UUID with its five groups.
_ADDRESS_RE = re.compile(
    r"https?://\S+|(?<![0-9A-Za-z])[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}(?![0-9A-Za-z])",
    re.IGNORECASE,
)
_WORD_RE = re.compile(r"[A-Za-z0-9_]+")
# Names joined by `/` or `\`, read from the first name so a long run costs one pass:
# group 1 is the directories, group 2 the last name.
_SLASHED_RE = re.compile(r"(?<![\w.~+@/\\-])((?:[\w.~+@-]*+[/\\])++)([\w.+@-]*+)")
# The last name is a file's when it has an extension (`query.py`, `main.rs:5`).
_EXTENSION_RE = re.compile(r"\.[A-Za-z]\w*+(?![\w.])")
# What stands before a word read off an object (`qs.only`, `f().only`, `a[0].only`), and
# before a word inside a sentence.
_ATTRIBUTE_RE = re.compile(r"[A-Za-z0-9_)\]]\.")
_MID_SENTENCE_RE = re.compile(r"[a-z0-9,] ")


@dataclass(frozen=True)
class TrimPlan:
    """What the two legs read for one question. `active` False = raw inputs."""

    lexical_query: str
    embed_text: str
    tokens: list[str]
    active: bool = False
    tokens_in: int = 0
    all_tokens: list[str] = field(default_factory=list)
    keep: frozenset[str] = frozenset()
    said: dict[str, int] = field(default_factory=dict)
    own: frozenset[str] = frozenset()
    named: frozenset[str] = frozenset()
    cap: int = LEXICAL_TOKEN_CAP
    boilerplate_dropped: int = 0


def strip_boilerplate(text: str) -> tuple[str, int]:
    """The text without the template headings, and how many were removed.
    Whitespace runs left by a removal collapse to one space."""
    cleaned, n = _BOILERPLATE_RE.subn(" ", text)
    if n == 0:
        return text, 0
    return " ".join(cleaned.split()), n


def identifier_forms(text: str) -> frozenset[str]:
    """Lowercased whole forms of the identifiers in `text` that a trim must
    keep: camel / snake compounds (tokenize.compound_identifiers) and every
    side of a `a::b` qualified name. A piece of an id (`7c05`, `ff00aa`, a hash) splits
    at its letter-digit edges like an identifier but is not one: it is a word of the
    question, kept as a word is."""
    keep = {t for t in compound_identifiers(text) if not is_id_piece(t)}
    for m in _QUALIFIED_RE.finditer(text):
        for part in m.group(1).split("::"):
            if len(part) >= 2:
                keep.add(part.lower())
    return frozenset(keep)


def is_id_piece(word: str) -> bool:
    """A lowercased word of four or more hex characters where letters and digits meet at
    two or more places (`7c05`, `ff00aa`, `2eed554`): a group of an id, a colour or a
    hash. A word and a number (`f401`, `e164`, `ed25519`) meet at one and are a name."""
    return (
        bool(_HEX_RE.fullmatch(word))
        and sum(a.isdigit() != b.isdigit() for a, b in zip(word, word[1:], strict=False)) >= 2
    )


def is_hash(word: str) -> bool:
    """A piece of an id of `HASH_MIN_CHARS` or more characters with two or more digits:
    a commit or fiddle hash. A shorter piece and a word around one digit (`face2face`)
    stay words of the question; a number stays a number."""
    return len(word) >= HASH_MIN_CHARS and sum(c.isdigit() for c in word) >= 2 and is_id_piece(word)


def strip_non_terms(text: str) -> str:
    """The text without what names no subject: URLs, UUIDs and hashes. A hash or a UUID
    goes whole, before the tokenizer splits it at its hyphens and letter-digit edges
    into pieces that read as rare words."""
    return _WORD_RE.sub(
        lambda m: " " if is_hash(m.group(0).lower()) else m.group(0),
        _ADDRESS_RE.sub(" ", text),
    )


def written_as_code(text: str, start: int, end: int) -> bool:
    """Whether the word `text[start:end]` is written as code and not as prose: called
    (`only(`), read off an object (`qs.only`), alone in backticks, or capitalised where
    no sentence starts (`returns None`, `the WHERE clause`)."""
    before, after = text[max(start - 2, 0) : start], text[end : end + 1]
    return (
        after == "("
        or bool(_ATTRIBUTE_RE.fullmatch(before))
        or (before.endswith("`") and after == "`")
        or (text[start].isupper() and bool(_MID_SENTENCE_RE.fullmatch(before)))
    )


def is_file_path(directories: str, name: str) -> bool:
    """Whether names joined by `/` or `\\` are a file path and not words set side by side
    (`request/response`, `and/or`, `1/8`): it starts at a root (`/usr/lib`, `./manage.py`,
    `~/src`, `\\Users` after a drive) or ends in a file name with an extension. A full
    stop after the name ends the sentence, not the name (`in db/models/query.py.`)."""
    return directories[0] in "/\\~." or bool(_EXTENSION_RE.search(name.rstrip(".")))


def term_counts(text: str) -> dict[str, int]:
    """How many words of `text` hold each lexeme, whole or as a subtoken. A function
    word counts only where it stands alone and is written as code: prose repeats it by
    grammar, and so do the identifiers that hold it (`order_by`, `run_from_argv`). One
    the question never says that way is in the result at 0. The directories of a file
    path are an address and are not counted, however many paths hold them: a lexeme
    only they hold is in the result at 1. The file's own name is counted."""
    counts: Counter[str] = Counter()
    directories: set[str] = set()

    def without_directories(m: re.Match[str]) -> str:
        if not is_file_path(m.group(1), m.group(2)):
            return m.group(0)
        directories.update(code_tokens(m.group(1)))
        return "  " + m.group(2)

    text = _SLASHED_RE.sub(without_directories, text)
    for m in _WORD_RE.finditer(text):
        whole = m.group(0).lower()
        for t in code_tokens(m.group(0)):
            if t not in _FUNCTION_WORDS:
                counts[t] += 1
            else:
                counts[t] += t == whole and written_as_code(text, *m.span())
    for t in directories:
        counts[t] = max(counts[t], int(t not in _FUNCTION_WORDS))
    return dict(counts)


def select_terms(
    tokens: list[str],
    df: dict[str, int],
    n_chunks: int,
    k: int,
    *,
    keep: frozenset[str],
    said: dict[str, int],
    own: frozenset[str] = frozenset(),
    named: frozenset[str] = frozenset(),
) -> list[str]:
    """At most `k` tokens for the lexical gate, in their first-seen order: the whole
    forms of the question's identifiers (`keep`) and, for `WORD_SHARE` of `k`, its words:
    those `said` more than once first, then the others, each in reading order. A word
    more than `COMMON_SHARE` of `n_chunks` chunks hold ranks after the other words,
    and one `said` counts at 0 (a function word the question never writes as code)
    after those; a bare number and an unknown token (df 0) rank after everything the
    corpus knows. Past `k` the words it repeats go first, then its identifiers, the
    rarest by document frequency first, then the words it says once. The lexemes only
    a caller's terms hold (`own`, the whole forms of their identifiers in `named`)
    take the slots that are left, the identifiers first, the rarest first. `said`
    counts the question alone, and a lexeme it says is its word whether or not a
    caller's term holds it too, so the terms of the question that the corpus knows
    are the same with and without a caller's."""
    k = max(int(k), 1)
    position = {t: i for i, t in enumerate(tokens)}

    def unknown(t: str) -> bool:
        return df.get(t, 0) <= 0 or t.isdigit()

    def common(t: str) -> bool:
        return df.get(t, 0) > n_chunks * COMMON_SHARE

    def repeated(t: str) -> bool:
        return said.get(t, 0) >= 2 and not common(t)

    def order(t: str) -> tuple[bool, bool, bool, bool, int]:
        return (unknown(t), said.get(t) == 0, common(t), said.get(t, 0) < 2, position[t])

    theirs = own - said.keys()
    words = sorted((t for t in tokens if t not in keep and t not in theirs), key=order)
    kept_words = {t: i for i, t in enumerate(words[: max(int(k * WORD_SHARE), 1)])}

    def rank(t: str) -> tuple[bool, int, int, int]:
        if t in kept_words:
            return (unknown(t), 0 if repeated(t) else 2, kept_words[t], 0)
        tier = 1 if t in keep else 3 if t in named else 4
        return (unknown(t), tier, df.get(t, 0), position[t])

    eligible = [t for t in tokens if t in kept_words or t in keep or t in own]
    chosen = set(sorted(eligible, key=rank)[:k])
    return [t for t in tokens if t in chosen]


def embed_text_for(cleaned: str, max_words: int = EMBED_MAX_WORDS) -> str:
    """The first `max_words` whitespace words of the cleaned text."""
    words = cleaned.split()
    return cleaned if len(words) <= max_words else " ".join(words[:max_words])


def plan(
    query: str, extra_terms: list[str] | None, max_tokens: int, work: dict[str, int]
) -> TrimPlan:
    """Decide the two legs' inputs. `max_tokens` (the deployment's
    code_search_query_max_tokens) <= 0 turns the trim off;
    otherwise a question whose tokenizer output exceeds it is stripped of
    boilerplate, its vector text is the cleaned first page, and its lexical
    gate will take at most `min(max_tokens, LEXICAL_TOKEN_CAP)` tokens once the
    corpus statistics arrive (select_terms): its identifiers and its words, those
    it repeats first. `extra_terms` are a caller's own choice of terms, not words of
    the question: none is stripped or filtered or counted as said, and they are asked
    for in the slots the question leaves. `work` receives the disclosure counters only
    when the trim is active."""
    lexical_query = query if not extra_terms else query + " " + " ".join(extra_terms)
    raw_tokens = code_tokens(lexical_query)
    cap = int(max_tokens)
    if cap <= 0 or len(raw_tokens) <= cap:
        return TrimPlan(lexical_query, query, raw_tokens[:LEXICAL_TOKEN_CAP])
    cleaned_q, dropped = strip_boilerplate(query)
    extra = " ".join(extra_terms or ())
    asked = strip_non_terms(cleaned_q)
    all_tokens = code_tokens(asked + " " + extra)
    if not all_tokens:  # only addresses and hashes: they are all the question says
        asked, all_tokens = cleaned_q, code_tokens(cleaned_q)
    work["query_trimmed_tokens_in"] = len(raw_tokens)
    work["query_trimmed_boilerplate_lines_dropped"] = dropped
    return TrimPlan(
        lexical_query=asked if not extra else asked + " " + extra,
        embed_text=embed_text_for(cleaned_q),
        tokens=all_tokens[:LEXICAL_TOKEN_CAP],
        active=True,
        tokens_in=len(raw_tokens),
        all_tokens=all_tokens,
        keep=identifier_forms(asked),
        said=term_counts(asked),
        own=frozenset(code_tokens(extra)),
        named=identifier_forms(extra),
        cap=min(cap, LEXICAL_TOKEN_CAP),
        boilerplate_dropped=dropped,
    )
