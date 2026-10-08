"""Pure classifiers over the question text and its parsed intent: which shape
a question has (count, set, when, chain, inference, affect, preference,
recommendation, mention-time, decisive, latest-word, multi-hop) decides the
retrieval legs, the gate override and the synthesis notes."""

from __future__ import annotations

import re
from typing import Any

# An inference question asks what is likely, not what was said
# — "Would Dana go on another roadtrip soon?", "Would Priya be
# considered religious?", "What job might Lena pursue?". Most
# open-domain misses open with "the memories do not record whether …" and then listed the very
# evidence the gold answer is inferred from ("Likely no; since this one went
# badly"). The memories are the evidence; the answer is the inference.
_INFERENCE_RE = re.compile(
    r"^\s*(?:would|might|could|will|is it likely|how likely|do you think|does .{1,40} seem|"
    r"(?:what|which|who|where|how) .{0,40}\b(?:might|would|could)\b|"
    r"(?:is|are|was|were) .{1,60}\b(?:likely|probably|considered)\b)",
    re.IGNORECASE,
)


def is_inference_question(query: str) -> bool:
    """Whether the question asks for a likelihood or an inference rather
    than a stated fact (a would/might/could head, or 'is X likely/considered
    …'). Pure."""
    q = (query or "").strip()
    return len(q) >= 12 and bool(_INFERENCE_RE.search(q))


# A "when" question whose memory carries an UNRESOLVED relative
# phrase — "John and Max had a camping trip last summer" dated [2023-08-11],
# "Max was part of the family for 10 years" dated [2023-06-03] — is often
# answered "the memories do not record when …" with the fact in the top ten
# (about half the temporal and single-hop conversation-QA misses). The extractor
# resolves "yesterday" and "last month"; a season,
# a duration or "a few years ago" reaches the reader unresolved, and the
# reader refuses instead of deriving.
_WHEN_RE = re.compile(
    r"^\s*(?:when\b|how long\b|how many (?:days|weeks|months|years)\b|"
    r"(?:in|on|at) (?:what|which) (?:year|month|day|date|season)\b|what (?:year|month|date|time)\b|"
    r"which (?:year|month|day)\b|how old\b|since when\b)",
    re.IGNORECASE,
)

# An amount superlative ("which store did I spend the most at",
# "which platform gained the most followers") is answered from a ranked list
# of (entity, amount) pairs read deterministically off the pool rows, so the
# synthesizer ranks numbers it can see instead of prose it paraphrases (a
# grocery-spend answer led with Walmart $120 while itself listing Thrive
# Market $150 two lines later).
_SUPERLATIVE_Q_RE = re.compile(
    r"\b(most|least|highest|lowest|biggest|largest|smallest|longest|shortest|cheapest|"
    r"priciest|max(?:imum)?|min(?:imum)?)\b",
    re.IGNORECASE,
)
_MEASURE_Q_RE = re.compile(
    r"\b(spen[dt]|cost|paid|pay|price|earn\w*|gain\w*|followers?|money|dollars?|hours?|"
    r"minutes?|miles|km|kg|lbs?|weight|calories|steps|points|views|subscribers)\b|\$",
    re.IGNORECASE,
)


def is_amount_superlative_question(query: str) -> bool:
    q = query or ""
    return bool(_SUPERLATIVE_Q_RE.search(q) and _MEASURE_Q_RE.search(q))


# A question whose answer is arithmetic over dated rows —
# "how many days passed between X and Y", "who did I meet first, A or B",
# "how long after X did Y happen". The similarity gate measures how much a
# row resembles the question; two dated rows about different events each
# resemble half of it, and the gate abstains with both in hand (headphone
# 03-08 and necklace 03-15 in the pool, "15/40 below floor", zero synthesis
# calls). The synthesizer keeps the right to say the
# memories are silent; the gate should not say it for it.
_ARITHMETIC_Q_RE = re.compile(
    r"\b(?:how (?:many|much) (?:days|weeks|months|years|time|hours|minutes)|"
    r"how long (?:ago|after|before|between|since|until|did|had|was|has|have)|"
    r"(?:days|weeks|months|years|hours|minutes) (?:passed|elapsed|between|apart|"
    r"after|before)|between the (?:day|time|date)|"
    r"(?:who|what|which|when)\b[^?]{0,80}\b(?:first|earlier|earliest|later|latest|"
    r"most recent(?:ly)?|last)\b[^?]{0,40}\bor\b|"
    r"(?:before|after|earlier|later) (?:or|than) (?:before|after|earlier|later|the)|"
    r"which (?:came|happened|was|occurred|did i do) (?:first|earlier|later|last))",
    re.IGNORECASE,
)


_HOW_MANY_Q_RE = re.compile(r"\bhow (?:many|much)\b", re.IGNORECASE)


def is_arithmetic_question(query: str) -> bool:
    """A question whose answer is a difference or an ordering of dates."""
    return bool(query) and bool(_ARITHMETIC_Q_RE.search(query))


def is_span_total_question(query: str) -> bool:
    """ "How many years in total did I spend…", "total number of months…" —
    a sum of durations across stages. Pure."""
    q = (query or "").lower()
    if not re.search(r"\b(years?|months?|weeks?|days?)\b", q):
        return False
    return bool(
        re.search(r"\b(in total|total|altogether|combined|overall|all together)\b", q)
        and re.search(
            r"\bhow (?:many|long)\b|\btotal (?:number of )?(?:years|months|weeks|days)\b", q
        )
    )


def is_when_question(query: str) -> bool:
    """Whether the question asks for a time — when, how long, how many years,
    what year/month — as opposed to what or who. Pure."""
    return bool(_WHEN_RE.search((query or "").strip()))


# A chain question names its answer through another entity's
# attribute — "the citizenship of the CEO of Twitter", "where the spouse of
# Igor of Kiev died", "the capital of the country where X was born". One
# embedding of the whole sits between the hops: the first-hop memory ranks
# (the CEO is Bernard Arnault) and the second-hop memory (Arnault's
# citizenship) never enters the pool because the question never says his
# name. On multi-hop questions over updated facts the second-hop fact
# was in the store for the answers that said "the memories do not state …".
_CHAIN_LINK_RE = re.compile(
    r"\b(?:of|for|by|from|whose|whom|where|which|that|with)\b", re.IGNORECASE
)
_CHAIN_HEAD_RE = re.compile(
    r"^\s*(?:what|which|who|where|when|in which|on which|to which|how)\b", re.IGNORECASE
)


def is_chain_question(query: str) -> bool:
    """Whether the question asks for an attribute of an entity it does not
    name but describes through another attribute (two genitive/relative
    links after a wh-head). Pure."""
    q = (query or "").strip()
    head = _CHAIN_HEAD_RE.search(q)
    if len(q) < 20 or not head:
        return False
    # Two relational links after the wh-head: "the citizenship OF the CEO OF
    # Twitter", "the partner OF Laura Bush associated WITH". One link ("the
    # CEO of Twitter") is a single hop.
    return len(_CHAIN_LINK_RE.findall(q[head.end() :])) >= 2


_MENTION_RE = re.compile(
    r"\b(?:i|we|you)\s+(?:mentioned|told you|talked about|discussed|brought up|said|"
    r"asked (?:you )?about|shared|described)\b|\bwe (?:talked|spoke|chatted)\b|"
    r"\bmentioned\b",
    re.IGNORECASE,
)
_RELATIVE_TIME_RE = re.compile(
    r"\b(?:a couple of|a few|several|two|three|four|five|six|seven|eight|nine|ten|\d+)\s+"
    r"(?:days?|weeks?|months?)\s+ago\b|"
    r"\b(?:yesterday|last week|the other day|earlier this week|last time)\b",
    re.IGNORECASE,
)


def is_mention_time_question(question: str) -> bool:
    """ "I mentioned X a couple of days ago — what was it?": the question is
    about WHEN something was said, so the transcript clock, not the event
    date, places the rows. Pure."""
    q = question or ""
    return bool(_MENTION_RE.search(q) and _RELATIVE_TIME_RE.search(q))


# A question about a bug, error or flaky failure — the shape
# whose decisive chunk (root cause, fix PR, backtrace) ranks at the tail.
_DECISIVE_Q_RE = re.compile(
    r"\b(errors?|logical_error|exceptions?|flaky|timeouts?|timed out|crash(?:es|ed)?|bugs?|"
    r"regressions?|failures?|failing|fix(?:es|ed)?|root cause|backtrace|stack trace|hangs?|"
    r"livelock|deadlock|overflow|segfault|asserts?|wrong results?)\b",
    re.IGNORECASE,
)

# A question about the current position — a disposition, a
# classification, a premise ("is X a known …"), how something should be done.
# Only the shapes where an earlier position and a later
# one compete — a premise ("is X a known …"), a classification, a standing
# disposition or status, a pending decision, "still" / "current" / "latest".
# The first shape (any directive / expectation / "how should" question) was
# measured null: it also fired on set questions, where the newest rows are
# not the decisive ones.
_LATEST_Q_RE = re.compile(
    r"\b(classif\w*|standing (?:disposition|decision|rule)|dispositions?|"
    r"pending (?:design )?decisions?|current(?:ly)? (?:status|state|disposition|position|verdict)|"
    r"still (?:open|blocked|pending|valid|current|true|the case)|"
    r"latest (?:verdict|finding|position|status|word)|superseded|"
    r"what is the (?:status|state|verdict))\b",
    re.IGNORECASE,
)
_PREMISE_Q_RE = re.compile(r"^\s*(?:is|are|was|were|does|do|did|has|have)\b", re.IGNORECASE)


def is_decisive_question(query: str) -> bool:
    """Whether the question is about a bug, error or flaky failure. Pure."""
    return bool(_DECISIVE_Q_RE.search(query or ""))


def is_latest_word_question(query: str) -> bool:
    """Shape test for a premise, classification, standing
    disposition, pending decision or current-state question — the shapes where
    a later-dated position supersedes an earlier one. Pure."""
    q = query or ""
    premise = bool(_PREMISE_Q_RE.match(q)) and bool(re.search(r"\bknown\b", q, re.IGNORECASE))
    return premise or bool(_LATEST_Q_RE.search(q))


def _parse_options_safe(question: str) -> dict[str, str]:
    try:
        from server.memory.domain.ask_option_guard import parse_options

        return parse_options(question or "")
    except Exception:  # noqa: BLE001 — a parser error is not an abstain reason
        return {}


_TEMPORAL_OPS = frozenset({"date_span", "count"})
# The intent grammar abstains on most free-form temporal questions ("how long
# did X take", "what changed since March", "which came first"), so the layout
# also reads the question itself. Ordering only — never a filter — so a false
# positive costs nothing but a different row order.
_TEMPORAL_CUE_RE = re.compile(
    r"\b(when|how long|since|before|after|first|then|timeline|history|sequence|"
    r"in order|chronolog\w*|changed|evolv\w*|over time|progress\w*|earliest|oldest)\b",
    re.I,
)
_LATEST_CUE_RE = re.compile(r"\b(latest|most recent|current(ly)?|now|newest|last)\b", re.I)

_OPTION_LINE_COUNT_RE = re.compile(r"(?m)^\s*\(?[A-Ha-h][\)\.:]\s+\S")


def _option_lines(question: str) -> int:
    return len(_OPTION_LINE_COUNT_RE.findall(question or ""))


# Recommendation-shaped questions: the judge — and the user —
# score the answer on whether it uses what is known about THEM, not on the
# quality of generic advice. Ground first, then tailor.
_RECOMMEND_CUE_RE = re.compile(
    r"\b(recommend|recommendations?|suggest(ions?)?|what should i|which .{0,40}\bshould i\b|"
    r"any (\w+ )?(tips|ideas|advice)|(have|got) (any |some )?(\w+ )?tips|tips (for|on) |"
    r"help me (choose|pick|decide|plan)|what would (suit|fit|work for) me|"
    r"ideas? for)\b",
    re.I,
)


def is_recommendation_question(question: str) -> bool:
    return bool(_RECOMMEND_CUE_RE.search(question or ""))


_AFFECT_CUE_RE = re.compile(
    r"\b(sentiment|emotion|emotional state|mood|"
    r"how (did|do|does|was|is) (i|the user|they|she|he) feel|"
    r"feeling|felt|feel about)\b",
    re.IGNORECASE,
)


def is_affect_intent(question: str) -> bool:
    """A question about how the user felt (sentiment/mood/emotion), typically
    at or around a moment in time."""
    return bool(_AFFECT_CUE_RE.search(question or ""))


_PREFERENCE_CUE_RE = re.compile(
    r"\b(prefer|preference|preferences|favou?rite|what kind of|what type of|what sort of|"
    r"which genre|taste in|do i (like|enjoy)|might i (like|enjoy|prefer))\b",
    re.IGNORECASE,
)
_COMPOUND_SPLIT_RE = re.compile(
    r"(?<=[?.!])\s*(?:additionally|in addition|also|and also|furthermore|moreover)[,:]?\s+",
    re.IGNORECASE,
)


_MULTIHOP_CUE_RE = re.compile(
    r"\b(before|after|since|until|between|both|each|compare|compared|than|older|younger|"
    r"earlier|later|first|last|longest|shortest|most|least|how long|how many (?:times|years|"
    r"months|days)|which of|who (?:else|also)|same|different|together|while|"
    r"when .* (?:did|was|were)|the (?:one|person|place|thing) (?:who|that|which))\b",
    re.IGNORECASE,
)

# Date-of-event questions. On a 106-question conversation-QA pair
# where `always` beat `off` by 6.6 points, 7 of the 9 questions it fixed were
# plain "When did X do Y?" lookups — not multi-hop by any reading, and none
# matched the cue regex above. What the split buys them is a second retrieval
# for the EVENT ("Did Priya give a speech at a school?") whose row carries
# the date, where the whole question's embedding is dominated by "when".
_DATE_OF_EVENT_RE = re.compile(
    r"^\W*(?:when (?:did|was|were|is|are|do|does|has|have|had|will)\b|"
    r"(?:on |in |at )?(?:what|which) (?:date|day|month|year|time)\b|"
    r"how long ago\b|how many (?:days|weeks|months|years) (?:ago|since|until|before|after)\b)",
    re.IGNORECASE,
)


def looks_temporal(question: str, intent: Any = None, order_mode: str = "") -> bool:
    """The question reads time — a date-of-event shape, a
    temporal cue, a relative date the grammar resolved, or an ordering the
    evidence layer already made chronological. LLM-free."""
    q = (question or "").strip()
    if order_mode in ("chronological", "reverse_chronological"):
        return True
    if intent is not None and bool(getattr(intent, "resolved_dates", None)):
        return True
    if not q:
        return False
    return bool(_DATE_OF_EVENT_RE.search(q) or _TEMPORAL_CUE_RE.search(q))


def looks_multihop(question: str) -> bool:
    """Cheap cue gate for `decompose="auto"`: comparatives, sequencing,
    aggregation over several facts, a relative clause naming a thing by
    another fact, or a date-of-event question. Long
    questions with two clauses count too."""
    q = (question or "").strip()
    if not q:
        return False
    if _MULTIHOP_CUE_RE.search(q) or _DATE_OF_EVENT_RE.search(q):
        return True
    words = q.split()
    return len(words) >= 14 and (" and " in q.lower() or "," in q)


# Each part beyond the first is one more memory.search on the ask, run in
# sequence; a 2,000-character stem of "…? Also, …" joints would otherwise fan
# out to hundreds. The tail past the cap stays in the last part.
COMPOUND_MAX_PARTS = 4


def split_compound_question(stem: str, *, max_parts: int = COMPOUND_MAX_PARTS) -> list[str]:
    """The sub-questions of a stem joined by 'Additionally,' / 'Also,' after a
    question mark ("How many X? Additionally, how many Y?"), at most
    `max_parts` of them. One element for a plain question. Each part is
    retrieved on its own so the second half is not answered from the first
    half's window."""
    text = (stem or "").strip()
    if not text:
        return []
    if max_parts < 2:
        return [text]
    split = _COMPOUND_SPLIT_RE.split(text, maxsplit=max_parts - 1)
    parts = [p.strip() for p in split if p and p.strip()]
    return parts if len(parts) > 1 else [text]


def is_preference_intent(intent: Any, question: str) -> bool:
    """A question about what the user tends to like: the grammar's
    `preference_attribute`, or a preference cue in the stem."""
    if intent is not None and str(getattr(intent, "preference_attribute", "") or ""):
        return True
    return bool(_PREFERENCE_CUE_RE.search(question or ""))


def _count_parts_in_pool(question: str, hits: list[Any]) -> bool:
    """True when the question counts a set and at least one retrieved row
    names a part of the counted category: "total number of
    siblings" with a "3 sisters" row, "how many pets" with a "dog" row."""
    from server.memory.domain import set_expansion as _se

    if not _se.is_set_question(question or ""):
        return False
    terms = _se.collective_terms(question or "")
    if not terms:
        # No collective noun: the head noun itself ("how many books") must appear.
        m = re.search(r"\b(?:how many|number of)\s+([a-z][a-z\-']+)", (question or "").lower())
        if not m:
            return False
        head = m.group(1)
        terms = [head, head[:-1] if head.endswith("s") else head + "s"]
    for h in hits[:40]:
        text = str(getattr(h, "content_excerpt", "") or "").lower()
        if any(re.search(rf"\b{re.escape(t)}s?\b", text) for t in terms):
            return True
    return False


def is_total_number_question(question: str) -> bool:
    """ "What is the total number of X …" is a count; the
    intent parser keys on "how many"."""
    return bool(re.search(r"\b(?:total\s+)?number\s+of\s+[a-z]", (question or "").lower()))


def is_count_intent(intent: Any) -> bool:
    """A question the parser classified as a row count ("how many X ...").
    Stated-value updates ("how many hours do I sleep now") are not counts."""
    if intent is None:
        return False
    if getattr(intent, "stated_value_update", False):
        return False
    return getattr(intent, "op", "") == "count" or (
        getattr(intent, "query_shape", "") == "aggregational"
        and getattr(intent, "op", "") in ("", "count")
    )


_FIRST_PERSON_RE = re.compile(r"\b(I|I've|I'm|I'd|I'll|my|me|mine|we|our)\b")


def _is_first_person_question(question: str) -> bool:
    """A question about the user's own events, purchases or statements."""
    return bool(_FIRST_PERSON_RE.search(question or ""))
