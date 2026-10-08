"""Lexical, LLM-free coverage measures over a first pass: the turns and
sentences no fact covers, list-dense turns, and which sentences
can hold a fact at all. The coverage sweep and its gate read these.
"""

from __future__ import annotations

import re

from server.memory.app.extract.models import ExtractedFact


def uncovered_turn_indices(
    messages: list[dict[str, object]],
    facts: list[ExtractedFact],
    *,
    min_chars: int = 40,
) -> list[int]:
    """Indices of the current turns no extracted fact cites, long enough to
    hold a fact (`min_chars` of content). Pure."""
    cited: set[int] = set()
    for f in facts:
        cited.update(i for i in (f.source_message_indices or []) if isinstance(i, int))
    out: list[int] = []
    for idx, m in enumerate(messages):
        if idx in cited:
            continue
        content = str(m.get("content", "") or "").strip()
        if len(content) < min_chars:
            continue
        out.append(idx)
    return out


_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+|\n+")
_SWEEP_STOP = frozenset(
    {
        "the",
        "a",
        "an",
        "of",
        "in",
        "on",
        "at",
        "to",
        "is",
        "are",
        "was",
        "were",
        "be",
        "and",
        "or",
        "for",
        "with",
        "by",
        "as",
        "it",
        "its",
        "that",
        "this",
        "has",
        "have",
        "had",
        "from",
        "into",
        "his",
        "her",
        "their",
        "he",
        "she",
        "they",
        "user",
        "i",
        "my",
        "me",
        "we",
        "our",
        "you",
        "your",
        "just",
        "also",
        "really",
        "very",
        "so",
        "but",
        "not",
        "no",
        "yes",
    }
)


_SALIENT_RE = re.compile(r"(?<!^)(?<![.!?]\s)\b([A-Z][\w'\-]*|\d[\d.,:]*)")


def _salient_tokens(sentence: str) -> set[str]:
    """The tokens of a sentence that carry its value: capitalised words past
    the sentence's first word and numbers, normalised the way `_content_words`
    normalises ("Bengaluru" → {"bengaluru"}, "27:12" → {"27:12"})."""
    out: set[str] = set()
    text = (sentence or "").strip()
    for m in _SALIENT_RE.finditer(text):
        if m.start() == 0:
            continue
        out |= _content_words(m.group(1))
    return out


def _content_words(text: str) -> set[str]:
    out: set[str] = set()
    for w in re.findall(r"[a-z0-9][a-z0-9'\-]*", (text or "").lower()):
        w = w.strip("'-")
        if (len(w) < 3 and not w.isdigit()) or w in _SWEEP_STOP:
            continue
        for suf in ("ing", "ed", "es", "s"):
            if len(w) > 4 and w.endswith(suf):
                w = w[: -len(suf)]
                break
        out.add(w)
    return out


_LIST_ITEM_RE = re.compile(r"(?:(?<=\s)|^)(?:\d{1,4}[.)]|[-*\u2022])\s+(?=\S)")


def statement_dense_turns(
    messages: list[dict[str, object]], *, min_statements: int = 10, min_chars: int = 20
) -> int:
    """How many current turns are a LIST of
    `min_statements` or more items — numbered ("12. The capital of …", inline
    or one per line) or bulleted — each `min_chars` or longer. A fact list is
    such a turn; prose is not, however many sentences it holds: every dense
    session (a PR review, a code discussion) meets a plain sentence count, so
    counting sentences would fire the sweep on nearly every extraction and
    double extraction cost for a 15 % row yield. Pure."""
    if min_statements <= 0:
        return 0
    dense = 0
    for m in messages:
        content = str(m.get("content", "") or "").strip()
        if len(content) < min_statements * min_chars:
            continue
        marks = [mm.start() for mm in _LIST_ITEM_RE.finditer(content)]
        if len(marks) < min_statements:
            continue
        items = 0
        for i, pos in enumerate(marks):
            nxt = marks[i + 1] if i + 1 < len(marks) else len(content)
            text = content[pos:nxt].strip()
            text = _LIST_ITEM_RE.sub("", text, count=1).strip()
            if len(text) >= min_chars and not text.endswith(":"):
                items += 1
        if items >= min_statements:
            dense += 1
    return dense


def uncovered_statement_count(
    messages: list[dict[str, object]], facts: list[ExtractedFact], *, min_chars: int = 40
) -> int:
    """How many statements the first pass left
    without a memory — whole current turns no fact cites (`uncovered_turn_
    indices`) plus, inside cited turns, the sentences no fact shares two
    content words with (`uncovered_sentences`). The lexical, LLM-free
    coverage check that decides whether a conversation ingest gets the
    second reading. Pure."""
    turns = uncovered_turn_indices(messages, facts, min_chars=min_chars)
    sents = uncovered_sentences(messages, facts)
    return len(turns) + sum(len(v) for i, v in sents.items() if i not in set(turns))


def uncovered_sentences(
    messages: list[dict[str, object]],
    facts: list[ExtractedFact],
    *,
    min_chars: int = 20,
    min_sentences: int = 3,
    max_sentences: int = 60,
) -> dict[int, list[str]]:
    """Sentence granularity: inside a turn the first pass DID
    cite, the individual statements no fact shares two content words with.
    A turn that holds several statements ("Here is a list of facts: …", a
    long update) is "covered" by one fact while the others are skipped — on
    a 455-statement context one reading stored 437 rows, the next 327,
    the difference all statements inside cited turns. Only turns with `min_sentences` or
    more sentences are split (a one-line reply is the turn sweep's business); sentences
    shorter than `min_chars` are ignored. Pure."""
    fact_words = [_content_words(f.value) for f in facts if f.value]
    out: dict[int, list[str]] = {}
    total = 0
    for idx, m in enumerate(messages):
        content = str(m.get("content", "") or "").strip()
        if not content:
            continue
        # List numbering ("12. The capital of …") is not a sentence boundary,
        # and a header that introduces the list ("Here is a list of facts:")
        # carries no fact.
        body = re.sub(r"(?m)^\s*\d+[.)]\s+", "", content)
        sentences = [
            s.strip()
            for s in _SENTENCE_SPLIT_RE.split(body)
            if s and s.strip() and not s.strip().endswith(":")
        ]
        # Statement-dense means several sentences long enough to carry a
        # fact — "Wow! Great! I signed up for pottery." is one statement and
        # the turn sweep's business.
        long_sentences = [s for s in sentences if len(s) >= min_chars]
        if len(long_sentences) < min_sentences:
            continue
        missing: list[str] = []
        for sent in long_sentences:
            words = _content_words(sent)
            if len(words) < 2:
                continue
            # Covered means a fact carries (nearly) all of the sentence's
            # content words — not merely two of them: an update repeats the
            # template ("The chairperson of Fatah is …") and differs in the
            # object, and two shared words would call it covered (measured:
            # six updated pairs still lost with that rule). Three quarters
            # tolerates a dropped filler word ("the city of London" / "London")
            # — and lets a one-word update through: "Thomas Arne was born in the city of
            # Bengaluru" shares eight of nine words with the London fact (85 of one
            # corpus's 103 updates pass the three-quarters test against their
            # predecessor). So the covering fact must also carry every salient token of
            # the sentence — its proper nouns past the first word and its numbers —
            # which is where the new value sits.
            salient = _salient_tokens(sent)
            if any(
                len(words & fw) >= max(2, -(-3 * len(words) // 4)) and salient <= fw
                for fw in fact_words
            ):
                continue
            missing.append(sent)
            total += 1
            if total >= max_sentences:
                break
        if missing:
            out[idx] = missing
        if total >= max_sentences:
            break
    return out


_SWEEP_SPEAKER_PREFIX_RE = re.compile(r"^\w[\w'\-]*:\s*")
_SWEEP_PHOTO_CAPTION_RE = re.compile(r"\[shared a photo:.*\]", re.IGNORECASE)
_SWEEP_GREETING_RE = re.compile(
    r"^(?:hey|hi|hello|bye|goodbye|thanks?|thank you|wow|yeah|yep|nope|sure|cool|awesome|"
    r"great|nice|congrats|congratulations|good luck|take care|talk (?:to you )?soon|see you|"
    r"sounds (?:good|great|fun|amazing))\b[^a-z]*(?:\w+[!.,]*\s*){0,3}$",
    re.IGNORECASE,
)


def sentence_is_listable(sentence: str) -> bool:
    """A sentence the sweep lists has to be able to
    hold a fact. A question, a greeting or thanks, and a sentence with fewer
    than three content words cannot; a shared photo's caption can. Measured
    on five multi-session conversations: 336 of the 2,082 sentences the sweep
    listed were of the first three kinds and none held a gold instance; six
    gold instances lived in captions. Pure."""
    body = _SWEEP_SPEAKER_PREFIX_RE.sub("", (sentence or "").strip()).strip()
    if not body:
        return False
    if _SWEEP_PHOTO_CAPTION_RE.search(body):
        return True
    if body.endswith("?"):
        return False
    if _SWEEP_GREETING_RE.match(body):
        return False
    return len(_content_words(body)) >= 3
