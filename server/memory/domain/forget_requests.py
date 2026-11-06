"""A user's request to forget: recognising it in a stored fact or a message,
the replacement facts it states, and which stored rows it names."""

from __future__ import annotations

import re
from typing import Any

_FORGET_VALUE_RE = re.compile(
    r"\b(asked|asks|requested|requests|told|wants?|would like)\b[^.]{0,60}?"
    r"\b(to )?(forget|not (to )?(remember|store|keep|use)"
    r"|stop (remembering|using|storing)|erase|delete)\b",
    re.IGNORECASE,
)
_FORGET_KEY_RE = re.compile(r"(^|_)(forget|forgotten|do_not_remember|dont_remember)(_|$)")


def is_forget_request(fact: dict[str, Any]) -> bool:
    """A request not to remember, however the extractor phrased it. The schema asks for
    `forget: true`, but the model often writes the request as a fact instead — "User
    asked to forget that they read the Bible daily", tagged `forget`, typed `preference`
    — storing the thing it was asked to drop. The tag, the key and the sentence shape
    are all accepted as the signal."""
    if fact.get("forget") is True:
        return True
    tags = {str(t).strip().lower() for t in (fact.get("tags") or [])}
    if "forget" in tags:
        return True
    if _FORGET_KEY_RE.search(str(fact.get("key") or "").lower()):
        return True
    return bool(_FORGET_VALUE_RE.search(str(fact.get("value") or "")))


_FORGET_STOPWORDS = frozenset(
    "a an the and or of to for in on at by with about from that this their they them "
    "his her its user users assistant asked asks requested requests told wants want would "
    "like please forget forgets forgotten remember remembering store storing keep keeping "
    "use using stop stops not no do does did be is are was were has have had it you your "
    "me my i we our memory memories preference preferences fact information "
    "likes liked enjoy enjoys enjoyed love loves loved prefer prefers preferred really "
    "usually often sometimes always tend tends out very quite earlier before ago "
    "previously once recently again anymore said mentioned shared".split()
)
_WORD_RE = re.compile(r"[a-z0-9]+")


def _content_tokens(text: str) -> set[str]:
    """Lower-cased content words of a sentence or key, request boilerplate and
    stopwords removed, light plural folding so 'performances' meets
    'performance'."""
    out: set[str] = set()
    for w in _WORD_RE.findall(str(text or "").lower()):
        if w in _FORGET_STOPWORDS or len(w) < 3:
            continue
        if len(w) > 4 and w.endswith("s"):
            w = w[:-1]
        out.add(w)
    return out


# A request to forget is read from the user's own message. The
# verb families that ask the assistant to drop something it knows — and none
# of the ones that ask it to change its behaviour ("stop using emojis") or
# to do something later ("don't forget to remind me").
_FORGET_MSG_PREFIX = (
    r"^(?:(?:please|kindly|also|and|hey|ok|okay|so|oh|actually|by the way|no wait|wait|no)[,\s]+)*"
    r"(?:(?:can|could|would|will)\s+you\s+(?:please\s+)?|"
    r"i(?:'d|\s+would)\s+(?:like|prefer|want)\s+you\s+to\s+|i\s+(?:want|need)\s+you\s+to\s+)?"
    r"(?:please\s+)?"
)
_FORGET_MSG_VERB = (
    r"(?:forget|disregard|unlearn|"
    r"(?:don'?t|do\s+not|never)\s+(?:remember|keep|store|retain|save|hold\s+on\s+to)|"
    r"stop\s+remembering|no\s+longer\s+remember|"
    r"never\s+mind(?=\s+what\s+i\s+(?:said|told|mentioned)))"
)
_FORGET_MSG_ERASE = (
    r"(?:erase|delete|remove|drop|discard|scrap|wipe|clear|purge)\s+"
    r"(?:(?:the|that|this|those|these|your|any|all|every|my)\s+)*"
    r"(?:memory|memories|note|notes|record|records|info|information|fact|facts|entry|entries|"
    r"detail|details|thing|things|bit|part|mention|mentions|reference|references|"
    r"what\s+(?:you\s+(?:know|have|remember|learned|learnt|stored|saved)|i\s+(?:said|told\s+you|mentioned|shared)))"
)
# Longest connector first: "what I told you about" must win over "what".
_FORGET_MSG_LINK = (
    r"(?:(?:what\s+i\s+(?:said|told\s+you|mentioned|shared)(?:\s+(?:earlier|before|once|previously))?"
    r"(?:\s+(?:about|regarding|concerning|that))?|"
    r"the\s+(?:memory|note|part|thing|bit|detail|info|information)\s+(?:of|about|that|where)|"
    r"the\s+fact\s+that|(?:anything|everything|all)\s+about|"
    r"(?:that\s+)?i\s+(?:said|told\s+you|mentioned)\s+(?:that\s+)?|"
    r"that|what|about|regarding|concerning|of|on)\s*)?"
)
_FORGET_MSG_RE = re.compile(
    _FORGET_MSG_PREFIX
    + "(?:"
    + _FORGET_MSG_VERB
    + r"\s+"
    + _FORGET_MSG_LINK
    + "|"
    + _FORGET_MSG_ERASE
    + r"\s+"
    + _FORGET_MSG_LINK
    + ")"
    + r"(?P<what>.+?)\s*$",
    re.IGNORECASE,
)
# "forget it", "forget about that, let's move on": no object to resolve.
_FORGET_MSG_PRONOUNS = frozenset("it this that them those these everything anything all".split())
# A longer sentence is not read as a request to forget.
MAX_FORGET_SENTENCE_CHARS = 400
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+|\n+")


# "forget that I prefer Neovim, I switched to VS Code" names
# the forgotten clause and then the replacement. The clause ends at a dash, a
# semicolon, or a comma that opens a new clause ("…, I / we / it's / now …");
# otherwise the replacement rides inside the clause, its words match the new
# fact, and the write step drops the truth it was asked to keep.
_REPLACEMENT_CUT_RE = re.compile(
    r"\s+[—–]\s*|\s+-\s+|\s*;\s*|"
    r",\s*(?=(?:i|i'm|i've|i'd|we|we're|we've|it|it's|its|my|our|now|they|he|she|"
    r"that's|the\s+new|instead|but|and\s+(?:i|we|now))\b)",
    re.IGNORECASE,
)


def split_replacement(clause: str) -> tuple[str, str]:
    """(what the user asked to forget, what the same sentence says instead)."""
    parts = _REPLACEMENT_CUT_RE.split(str(clause or ""), maxsplit=1)
    head = parts[0]
    rest = parts[1] if len(parts) > 1 else ""
    return head, rest.strip()


_REPLACEMENT_META = frozenset(
    "longer anymore wants wanted remembered stored saved kept retained tracked "
    "request requested asked assistant forget forgotten drop dropped delete deleted "
    "erase erased note notes".split()
)


def _new_content(value: str, against: list[str]) -> set[str]:
    """Content words of `value` that none of `against` holds."""
    have: set[str] = set()
    for text in against:
        have |= _content_tokens(text)
    return _content_tokens(value) - have - _REPLACEMENT_META


def replacement_facts(
    facts: list[dict[str, Any]],
    requests: list[dict[str, Any]],
    existing: list[dict[str, Any]],
    messages: list[dict[str, Any]],
    *,
    keep_said: bool,
) -> tuple[list[dict[str, Any]], set[str], set[str]]:
    """The facts that carry what the user said INSTEAD of what they asked to
    forget, the batch with any forget entry that also
    carries such a replacement split in two, and the rows those replacements
    link that the request names (retired: "forget the 30s cap, retries now
    cap at 60s" extracts as ONE fact linked to the 30s row, no forget entry).

    A fact is a replacement when it comes from the message that asked to
    forget and says something the forgotten rows do not: "User drinks light
    roast coffee now" after "Forget the dark roast thing — I drink light
    roast now". It is written even when its key is the forgotten one (the
    old row is retired, the new version is the live one) and it is never a
    content target of the request. A forget entry whose value carries words
    of the message's replacement clause ("User switched to VS Code from
    Neovim", `forget: true`) is the extractor folding both into one item; it
    is split into the request and a replacement fact. A pure request ("forget
    my salary") has no replacement clause, so nothing is split or kept."""
    by_key = {str(r.get("memory_key") or ""): str(r.get("value") or "") for r in existing or []}
    rest_text = forget_sentences(messages)
    rest_by_msg = {i: _content_tokens(t) for i, t in rest_text.items()}
    msgs = set(rest_by_msg)
    clauses = [str(r.get("value") or "") for r in requests]
    # Messages whose replacement is already an ordinary fact of the batch.
    kept_msgs: set[int] = set()
    for f in facts:
        src = {int(i) for i in (f.get("source_message_indices") or []) if str(i).isdigit()}
        if not is_forget_request(f) and src & msgs:
            base = [by_key[k] for k in (f.get("linked_keys") or []) if k in by_key]
            if _new_content(str(f.get("value") or ""), base or clauses):
                kept_msgs |= src & msgs
    out: list[dict[str, Any]] = []
    protected: set[str] = set()
    retire: set[str] = set()
    for f in facts:
        src = {int(i) for i in (f.get("source_message_indices") or []) if str(i).isdigit()}
        links = [k for k in (f.get("linked_keys") or []) if k in by_key]
        forgotten = [by_key[k] for k in links]
        if not is_forget_request(f):
            # Novel against the rows it replaces when it links them — the
            # user's clause may carry the replacement's own words when the
            # sentence has no cut ("the 30s cap, retries now cap at 60s").
            if src & msgs and _new_content(str(f.get("value") or ""), forgotten or clauses):
                protected.add(str(f.get("key") or ""))
                asked = set().union(*(_content_tokens(c) for c in clauses)) if clauses else set()
                retire.update(
                    k
                    for k in links
                    if asked & (_content_tokens(by_key[k]) | _content_tokens(k.replace("_", " ")))
                )
            out.append(f)
            continue
        rest = set().union(*(rest_by_msg.get(i, set()) for i in src)) if src else set()
        value = str(f.get("value") or "")
        novel = _new_content(value, forgotten + clauses) & rest
        if not rest or not novel:
            out.append(f)
            continue
        if _FORGET_VALUE_RE.search(value):
            out.append(f)  # a request that names the replacement: rescued below
            continue
        # A carrier's own text is the replacement, so its links are checked
        # against what the user asked to forget, not against that text: the
        # extractor linked "forget that, I prefer coffee" to the dark-roast row.
        asked_words = set().union(*(_content_tokens(c) for c in clauses)) if clauses else set()
        kept_links = [
            k
            for k in (f.get("linked_keys") or [])
            if k not in by_key
            or asked_words & (_content_tokens(by_key[k]) | _content_tokens(k.replace("_", " ")))
        ]
        # ...and the request resolves by what was asked to forget, never by
        # the replacement's words.
        out.append({**f, "value": " ".join(clauses), "linked_keys": kept_links})
        copy = {k: v for k, v in f.items() if k not in ("forget", "linked_keys")}
        copy["tags"] = [t for t in (f.get("tags") or []) if str(t).lower() != "forget"]
        copy["key"] = _FORGET_KEY_RE.sub("_", str(f.get("key") or "")).strip("_") or "user_update"
        out.append(copy)
        protected.add(copy["key"])
        kept_msgs |= src & msgs
    # Under an operator policy (`keep_said=False`) only the model, given the
    # policy, decides which of the user's words are written.
    if keep_said:
        said = _said_corrections(messages, kept_msgs, clauses + list(by_key.values()))
        out.extend(said)
        protected.update(str(f["key"]) for f in said)
    return out, protected, retire


def _said_corrections(
    messages: list[dict[str, Any]], kept_msgs: set[int], known: list[str]
) -> list[dict[str, Any]]:
    """The replacement no fact of the batch carries — the extractor folded it
    into a request ("…not to keep Neovim, as they are now on Emacs") or
    dropped it — kept in the user's own words, so a correction never ends with
    the old value retired and the new one nowhere. `kept_msgs`: the messages
    whose replacement a fact already carries; `known`: what the request named
    and the rows it may retire."""
    out: list[dict[str, Any]] = []
    for i, said in forget_sentences(messages, same_sentence=True).items():
        if i in kept_msgs or not said or not _FIRST_PERSON_RE.match(said):
            continue
        if not _new_content(said, known):
            continue
        key = f"user_said_{i}_" + "_".join(sorted(_content_tokens(said)))[:40].strip("_")
        out.append(
            {
                "key": key,
                "value": f'User said: "{said}"',
                "tags": ["correction"],
                "source_message_indices": [i],
            }
        )
    return out


def forget_requests_from_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Forget entries read from the user's own sentences:
    "please forget that I like scenic train journeys", "don't remember what
    I said about my salary", "delete the note about my ex". Each is a
    synthetic forget entry — `forget: true`, no key, the clause the user
    named as `value` — for `forget_targets` to resolve by content against
    the existing memories. A sentence that asks the assistant to do or
    change something ("don't forget to remind me", "stop using emojis") is
    not a request to forget; a clause with no content word ("forget it") is
    dropped here."""
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for m in messages or []:
        if str(m.get("role") or "").lower() != "user":
            continue
        text = str(m.get("content") or "")
        if not text:
            continue
        for sent in _SENTENCE_SPLIT_RE.split(text):
            sent = sent.strip().strip("\"'“”‘’")
            if not sent or len(sent) > MAX_FORGET_SENTENCE_CHARS:
                continue
            mt = _FORGET_MSG_RE.match(sent)
            if not mt:
                continue
            what = split_replacement(mt.group("what"))[0].strip().rstrip(".!?,;: ").strip()
            low = what.lower()
            if not what or low.startswith("to ") or low.startswith("about to "):
                continue
            head = re.split(r"[,;:!.\s]+", low, maxsplit=1)[0]
            if head in _FORGET_MSG_PRONOUNS:
                continue
            if not _content_tokens(what):
                continue
            if low in seen:
                continue
            seen.add(low)
            out.append({"key": "", "value": what, "forget": True, "tags": ["forget"]})
    return out


def forget_sentences(
    messages: list[dict[str, Any]], *, same_sentence: bool = False
) -> dict[int, str]:
    """Each user message holding a request to forget, mapped
    to what the same message says besides the forgotten clause — "forget
    that I prefer Neovim, I switched to VS Code" → "I switched to VS Code".
    A pronoun request ("forget that, I prefer coffee") counts here even though
    it names nothing to resolve: its message still carries the replacement."""
    out: dict[int, str] = {}
    for i, m in enumerate(messages or []):
        if str(m.get("role") or "").lower() != "user":
            continue
        text = str(m.get("content") or "")
        sents = [x.strip() for x in _SENTENCE_SPLIT_RE.split(text) if x.strip()]
        for raw in sents:
            sent = raw.strip("\"'“”‘’")
            # the length forget_requests_from_messages reads: the match is
            # quadratic in a sentence's trailing whitespace
            if len(sent) > MAX_FORGET_SENTENCE_CHARS:
                continue
            mt = _FORGET_MSG_RE.match(sent)
            if not mt:
                continue
            _, rest = split_replacement(mt.group("what"))
            others = "" if same_sentence else " ".join(x for x in sents if x is not raw)
            out[i] = " ".join(p for p in (out.get(i, ""), rest, others) if p).strip()
    return out


# A replacement clause the user states in the first person ("I switched to
# VS Code", "we deploy on Thursdays now", "it's 9:45") — not "let's move on".
_FIRST_PERSON_RE = re.compile(
    r"^(?:(?:and|so|now|but)\s+)?(?:i|i'm|im|i've|i'd|we|we're|we've|my|our|it's|it\s+is|"
    r"the\s+new|now)\b",
    re.IGNORECASE,
)


def forget_targets_by_content(
    fact: dict[str, Any], existing: list[dict[str, Any]], *, min_overlap: float = 0.5
) -> set[str]:
    """Existing memories a forget request is about, found by content when the
    extractor did not link them: the request's content
    words ("jazz blues performance") against each existing row's value and
    key; a row whose tokens cover at least `min_overlap` of the request's
    (and at least two of them) is a target. The candidates are the rows the
    pipeline already fetched as semantically relevant to this batch."""
    want = _content_tokens(fact.get("value") or "") | _content_tokens(
        _FORGET_KEY_RE.sub(" ", str(fact.get("key") or "").replace("_", " "))
    )
    if not want:
        return set()
    # A one-word request ("forget what I told you about my salary") resolves
    # only against rows keyed to the user; two or more words resolve anywhere.
    need = 2 if len(want) >= 2 else 1
    out: set[str] = set()
    for row in existing or []:
        key = str(row.get("memory_key") or "").strip()
        if not key:
            continue
        if need == 1 and not key.startswith("user_"):
            continue
        have = _content_tokens(row.get("value") or "") | _content_tokens(key.replace("_", " "))
        hit = len(want & have)
        if hit >= need and hit / len(want) >= min_overlap:
            out.add(key)
    return out


def _link_is_about(f: dict[str, Any], key: str, existing: list[dict[str, Any]] | None) -> bool:
    """A forget entry's link retires a row only when the row
    shares a content word with the request — measured, the extractor linked
    "forget my tea preference" to the editor row and "stop using emojis" to
    it too. A link to a row the batch does not show, or a request with no
    content word, is trusted."""
    row = next((r for r in existing or [] if str(r.get("memory_key") or "") == key), None)
    if row is None:
        return True
    want = _content_tokens(f.get("value") or "") | _content_tokens(
        _FORGET_KEY_RE.sub(" ", str(f.get("key") or "").replace("_", " "))
    )
    have = _content_tokens(row.get("value") or "") | _content_tokens(key.replace("_", " "))
    return not want or not have or bool(want & have)


def forget_targets(
    facts: list[dict[str, Any]],
    existing: list[dict[str, Any]] | None = None,
    protected: set[str] | frozenset[str] = frozenset(),
) -> set[str]:
    """The memory keys a batch's forget entries retire: each forget entry's
    own key, the same key without its `forget` marker (the extractor often
    keys the request `user_<attribute>_forget`), the existing memories it
    links (resolved to real keys by the extractor), and — when it linked
    none — the existing memories, or the facts of this same batch, that its
    content names. Empty when no entry asks to
    forget."""
    out: set[str] = set()
    for f in facts:
        if not is_forget_request(f):
            continue
        key = str(f.get("key") or "").strip()
        if key:
            out.add(key)
            bare = _FORGET_KEY_RE.sub(
                lambda m: "_" if m.group(1) and m.group(3) else "", key
            ).strip("_")
            bare = re.sub(r"_+", "_", bare)
            if bare and bare != key:
                out.add(bare)
        linked = [
            str(lk).strip()
            for lk in (f.get("linked_keys") or [])
            if str(lk or "").strip() and not str(lk).strip().startswith("__idx:")
        ]
        linked = [lk for lk in linked if _link_is_about(f, lk, existing)]
        out.update(linked)
        if not linked:
            # The premise and the request are often adjacent turns ("Since
            # you enjoy X…" / "Please forget that I enjoy X"), so the fact
            # the request names may be in this very batch, not yet written:
            # resolve against the batch's own facts too, and the write step
            # drops the ones named.
            candidates: list[dict[str, Any]] = list(existing or [])
            for g in facts:
                if g is f or is_forget_request(g):
                    continue
                gk = str(g.get("key") or "").strip()
                if gk in protected:  # the replacement is not a target
                    continue
                if gk:
                    candidates.append({"memory_key": gk, "value": g.get("value") or ""})
            if candidates:
                out.update(forget_targets_by_content(f, candidates))
    return out
