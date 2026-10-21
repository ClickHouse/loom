"""The entities a point write projects: the caller's own, or the ones mined from its
value without an LLM call."""

from __future__ import annotations

import logging
import re
import unicodedata
from collections.abc import Sequence
from typing import Any

from server.memory.domain import triples_follow_rows as tfr

log = logging.getLogger(__name__)


# --- Deterministic LLM-free entity projection for memory.set ---
# memory.set carries no `entities` column, so a point write is invisible to
# the graph until a bulk path or worker re-extracts it. This closes the gap
# without an LLM call: tokenize the value text into candidate phrases,
# canonicalize through the same `canonical_entity_text` as write_entity_edges
# (so write/read land on one node), and reuse write_entity_edges for the
# pairing / cap / temporal-window / insert path.

# Min length 3 skips "is/at/of" noise; the canonicalizer collapses i/me/my -> user.
_SET_ENTITY_MIN_LEN = 3
_SET_ENTITY_MAX_CANDIDATES = 24
# Words that cannot denote a thing: closed-class function words (prepositions,
# determiners, quantifiers, wh-words, conjunctions, pronouns) plus high-frequency
# degree/manner adverbs. Domain stopwords are deliberately NOT here —
# over-filtering drops real entities ("record", "show" as products; "bug", "fix"
# and "column" are legitimately the busiest nodes in a code-review namespace).
# Measured on a real entity graph without the later groups: `why` was the 7th
# highest-degree node of 19,897 (degree 19,083), with `all` (17,431), `explicitly` (10,009)
# and `what` (7,851) close behind, and the 90 words below held 5.4% of all edge degree while
# being 0.5% of nodes — function words appear in nearly every memory, so they pair with
# everything and concentrate degree far above their information content.
# These are canonical lowercase forms. A multi-word proper noun containing one
# ("Down Syndrome", "Per Andersson") still survives as a phrase node via
# _SET_ENTITY_PROPER_RE, so filtering the bare token costs no real entity.
_SET_ENTITY_STOPWORDS = frozenset(
    {
        # -- core closed-class set
        "the",
        "a",
        "an",
        "and",
        "or",
        "but",
        "if",
        "then",
        "else",
        "for",
        "of",
        "to",
        "in",
        "on",
        "at",
        "by",
        "with",
        "from",
        "as",
        "is",
        "are",
        "was",
        "were",
        "be",
        "been",
        "being",
        "am",
        "do",
        "does",
        "did",
        "has",
        "have",
        "had",
        "will",
        "would",
        "can",
        "could",
        "should",
        "may",
        "might",
        "must",
        "shall",
        "this",
        "that",
        "these",
        "those",
        "it",
        "its",
        "they",
        "them",
        "their",
        "he",
        "she",
        "his",
        "her",
        "we",
        "us",
        "our",
        "you",
        "your",
        "not",
        "no",
        "yes",
        "so",
        "than",
        "too",
        "very",
        "just",
        "also",
        "about",
        "into",
        "over",
        "after",
        "before",
        "when",
        "while",
        "because",
        "there",
        "here",
        # -- further prepositions and particles
        "near",
        "between",
        "under",
        "above",
        "through",
        "throughout",
        "along",
        "around",
        "across",
        "among",
        "against",
        "behind",
        "below",
        "beneath",
        "beside",
        "beyond",
        "during",
        "within",
        "without",
        "toward",
        "towards",
        "upon",
        "onto",
        "via",
        "per",
        "since",
        "until",
        "off",
        "out",
        "down",
        # -- determiners, quantifiers, wh-words, conjunctions
        "all",
        "any",
        "each",
        "every",
        "both",
        "some",
        "such",
        "same",
        "other",
        "another",
        "more",
        "most",
        "less",
        "least",
        "many",
        "much",
        "few",
        "several",
        "own",
        "none",
        "who",
        "whom",
        "whose",
        "which",
        "what",
        "where",
        "why",
        "how",
        "whether",
        "although",
        "though",
        "however",
        "therefore",
        "unless",
        "whereas",
        # -- degree and manner adverbs
        "only",
        "even",
        "still",
        "yet",
        "again",
        "once",
        "ever",
        "never",
        "always",
        "often",
        "sometimes",
        "usually",
        "already",
        "almost",
        "quite",
        "rather",
        "perhaps",
        "maybe",
        "instead",
        "indeed",
        "thus",
        "hence",
        "otherwise",
        "explicitly",
        "implicitly",
        "simply",
        "actually",
        "basically",
        "essentially",
        "generally",
        "specifically",
        "particularly",
        "especially",
        "currently",
        "previously",
        "recently",
        "initially",
        "finally",
        "eventually",
        "immediately",
        "directly",
        "exactly",
        "clearly",
        "obviously",
        "apparently",
        "possibly",
        "probably",
        "certainly",
        "definitely",
        # -- Korean pronouns and time words, as `_korean_stem` leaves them: "우리는" is
        # 우리 (we). 저, 나 and 너 are one syllable, too short to be a name already.
        "우리",
        "저희",
        "너희",
        "그녀",
        "그들",
        "당신",
        "지금",
        "오늘",
        "내일",
        "어제",
        # With 은 or 는, which `_korean_stem` leaves on 은: "오늘은" (today).
        "오늘은",
        "지금은",
        "내일은",
        "어제는",
    }
)
# Runs of TitleCase words are strong proper-noun candidates. Applied to the
# RAW value text before lowercasing so casing survives.
_SET_ENTITY_PROPER_RE = re.compile(r"\b[A-Z][\w'’.-]*(?:\s+[A-Z][\w'’.-]*)*")
# Longer than a name in wide characters: a run of unspaced text ("東京に住んでいます").
_SET_ENTITY_MAX_WIDE_LEN = 6  # not a setting: algorithm shape
# Longer than a name in Chinese characters alone: a sentence ("我住在北京"). A Chinese
# name is two to four ("北京", "毛泽东", "乌鲁木齐").
_SET_ENTITY_MAX_HAN_LEN = 4  # not a setting: algorithm shape
# Verb endings, which end no name: Korean "살아요" (live), "입니다" (am), Japanese
# "行きました" (went), "です" (is). A bare 요 is not one: "히데요" (Hideyo) is a name.
_VERB_ENDINGS = (
    "니다",
    "아요",
    "어요",
    "해요",
    "에요",
    "예요",
    "세요",
    "네요",
    "ます",
    "ました",
    "ません",
    "です",
    "でした",
    # Plain past and future endings: "먹었다" (ate), "좋았다" (was good), "가겠다"
    # (will go). 았, 었, 였 and 겠 are tense markers, in no common name.
    "았다",
    "었다",
    "였다",
    "겠다",
)
# Common grammar words written alone between spaces: Japanese particles, verbs, adverbs
# and greetings, Korean plain-form verbs, adjectives and adverbs. Only these, not a whole script or
# an ending: "さいたま" (Saitama) is hiragana and "캐나다" (Canada) ends in 다.
_GRAMMAR_WORDS = frozenset(
    {
        "こんにちは",
        "した",
        "する",
        "いる",
        "ある",
        "から",
        "まで",
        "では",
        "には",
        "でも",
        "ください",
        "ありがとう",
        "そして",
        "とても",
        "行った",
        "좋다",
        "같다",
        "많다",
        "좋아한다",
        "산다",
        "간다",
        "갔다",
        "있다",
        "없다",
        "했다",
        "한다",
        "하다",
        "이다",
        "본다",
        "만났다",
        "그리고",
        "정말",
        "너무",
        "가요",
        "와요",
        "봐요",
    }
)
# Korean particles that end no common name or noun: "서울에" (in Seoul), "저는" (I).
# 은, 이, 가, 의 and 도 are left on: they end names and nouns too (김하은, 고양이,
# 음악가, 민주주의, 제주도).
_KOREAN_PARTICLES = ("는", "를", "에", "을")
# Longer endings trimmed the same way: the particle 에서 ("서울에서", in Seoul) and the
# plain copula ("학생이다", is a student; "서울입니다", it is Seoul).
_KOREAN_LONG_ENDINGS = ("에서", "입니다", "이다")


def _is_hangul(char: str) -> bool:
    """A Hangul syllable: "서", "울"."""
    return "\uac00" <= char <= "\ud7a3"


def _is_han(char: str) -> bool:
    """A Chinese character (also used in Japanese and Korean): "北", "東"."""
    return "\u3400" <= char <= "\u9fff" or "\uf900" <= char <= "\ufaff"


def _korean_stem(word: str) -> str:
    """The noun of a Hangul word that ends in a particle or the copula: "서울에" and
    "서울에서" are 서울, "학생이다" is 학생. A word of two syllables keeps its ending
    ("마을", village), so "저는" stays whole and `_not_a_name` drops it."""
    if len(word) >= 3 and word.endswith(_KOREAN_PARTICLES) and _is_hangul(word[-2]):
        return word[:-1]
    for ending in _KOREAN_LONG_ENDINGS:
        stem = word[: -len(ending)]
        # A stem of two syllables or more: "아이다" (Aida) keeps its ending.
        if word.endswith(ending) and len(stem) >= 2 and all(_is_hangul(c) for c in stem):
            return stem
    return word


def _not_a_name(word: str) -> bool:
    """Shorter than a thing's name: "is", "at", "of", "3pm". Wide characters (CJK,
    Hangul) write a name in two: "東京", "서울", and five Chinese characters alone are
    a sentence ("我住在北京"). A verb ("살아요", "です", "먹었다"), a listed grammar word
    ("좋다", "こんにちは") or a one-syllable word and its particle ("저는", "집에") is
    not a name."""
    if not word[0].isalpha():
        return True
    if all(unicodedata.east_asian_width(c) in ("W", "F") for c in word):
        if len(word) < 2 or len(word) > _SET_ENTITY_MAX_WIDE_LEN:
            return True
        if len(word) > _SET_ENTITY_MAX_HAN_LEN and all(_is_han(c) for c in word):
            return True
        grammar = word in _GRAMMAR_WORDS or word.endswith(_VERB_ENDINGS)
        # Not 을: "마을" (village) and "가을" (autumn) are words.
        particle = len(word) == 2 and word.endswith(("는", "를", "에"))
        return grammar or particle
    return len(word) < _SET_ENTITY_MIN_LEN


def _candidate_entities_from_value(value: Any) -> list[str]:
    """Deterministic candidate-entity extraction from a stored memory value.

    Returns canonicalized entity texts (>= 2 chars), deduped,
    order-preserving, capped. Coerces value to text (dict/list via leaf
    scalars, skipping keys), grabs proper-noun phrases first, then stoplisted
    word tokens, and canonicalizes each so it lands on the same node a query
    seed would. Pure: no I/O, no LLM.
    """
    from server.memory.domain.graph_quality import canonical_entity_text

    surfaces: list[str] = []

    def _walk(v: Any) -> None:
        if isinstance(v, str):
            if v.strip():
                surfaces.append(v)
        elif isinstance(v, (int, float, bool)):
            return  # bare scalars carry no entity signal
        elif isinstance(v, dict):
            for vv in v.values():
                _walk(vv)
        elif isinstance(v, (list, tuple)):
            for vv in v:
                _walk(vv)

    _walk(value)
    text = " ".join(surfaces)
    if not text.strip():
        return []

    raw_candidates: list[str] = []
    for m in _SET_ENTITY_PROPER_RE.finditer(text):
        phrase = m.group(0).strip()
        if len(phrase) >= _SET_ENTITY_MIN_LEN:
            raw_candidates.append(phrase)
    for word in tfr.split_words(text, dots=True):
        tok = _korean_stem(word)
        if _not_a_name(tok):
            continue
        if tok.lower() in _SET_ENTITY_STOPWORDS:
            continue
        raw_candidates.append(tok)

    out: list[str] = []
    seen: set[str] = set()
    for raw in raw_candidates:
        canon = canonical_entity_text(raw)
        # Re-apply the stoplist AFTER canonicalization: a sentence-initial
        # "The" reaches the proper-noun grabber and canonicalizes to "the".
        if len(canon) < 2 or canon in seen or canon in _SET_ENTITY_STOPWORDS:
            continue
        seen.add(canon)
        out.append(canon)
        if len(out) >= _SET_ENTITY_MAX_CANDIDATES:
            break
    return out


def set_entities_for_projection(input: Any, value: Any) -> list[str]:
    """The graph nodes a point write projects. The caller's own `entities` win: they are what
    the writer said the row is about, and mining the text beside them would add every
    content word as a node. A trajectory transcript row is never text-mined — on one
    namespace 65% of all edges came from mining step transcripts, making `command`, `step`,
    `bash`, `after bash`, `revealed`, `content` its top entities. Otherwise the value is
    mined."""
    from server.memory.domain.transcript_rows import is_transcript_row

    declared = declared_entities(getattr(input, "entities", None))
    transcript = is_transcript_row(getattr(input, "tags", None), getattr(input, "source_trace", ""))
    if declared or transcript:
        return declared
    return _candidate_entities_from_value(value)


def declared_entities(entities: Sequence[Any] | None) -> list[str]:
    """The caller's own `entities`, canonical and capped, without mining the value."""
    from server.memory.domain.graph_quality import canonical_entity_text

    declared: list[str] = []
    for raw in entities or []:
        canon = canonical_entity_text(str(raw))
        if len(canon) >= 2 and canon not in declared:
            declared.append(canon)
    return declared[:_SET_ENTITY_MAX_CANDIDATES]
