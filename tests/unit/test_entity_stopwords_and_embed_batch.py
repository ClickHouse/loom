"""Two graph/embedding quality guards.

Both pin failures observed on real data, not hypotheticals.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from server.memory.app.set_entities import (  # noqa: E402
    _candidate_entities_from_value,
)
from server.memory.domain.graph_quality import canonical_entity_text  # noqa: E402


# --- entity candidate stoplist -------------------------------------------------
# Function words become hub nodes: on one real graph `why` was the 7th
# highest-degree node of 19,897, and 90 function words held 5.4% of all edge
# degree while being 0.5% of nodes. Each word below was observed as a real node.
@pytest.mark.parametrize(
    "word",
    [
        "why",
        "all",
        "what",
        "explicitly",
        "near",
        "which",
        "who",
        "where",
        "however",
        "actually",
        "currently",
        "specifically",
        "between",
        "through",
    ],
)
def test_function_words_are_not_entities(word: str) -> None:
    ents = _candidate_entities_from_value(f"The deployment {word} the shard replica failed")
    assert word not in ents, f"{word!r} must not become an entity graph node"


# The other half of the contract: the stoplist must stay closed-class. These are
# the busiest *legitimate* nodes in a code-review namespace, and the module's own
# comment warns that filtering them would drop real entities.
@pytest.mark.parametrize(
    "word", ["bug", "column", "coverage", "mergetree", "record", "show", "array"]
)
def test_domain_words_are_still_entities(word: str) -> None:
    ents = _candidate_entities_from_value(f"the {word} and the shard replica")
    assert word in ents, f"{word!r} is a real entity and must survive the stoplist"


def test_proper_noun_phrase_survives_even_when_it_contains_a_stopword() -> None:
    """Filtering the bare token must not cost the phrase node it appears in —
    this is why dropping these words loses no real entity."""
    ents = _candidate_entities_from_value("Alice reported Down Syndrome research")
    assert "down syndrome" in ents
    assert "down" not in ents


def test_measured_sentence_loses_only_the_function_word() -> None:
    """A probe sentence: `near` is not a node; the proper nouns and their
    constituents are, because the graph read matches
    seeds by exact string and dropping constituents would cost recall."""
    ents = _candidate_entities_from_value(
        "Zorbulax Quinteria met Fenwhistle Draymoor near Yalgamesh Peak"
    )
    assert "near" not in ents
    for kept in (
        "zorbulax quinteria",
        "fenwhistle draymoor",
        "yalgamesh peak",
        "zorbulax",
        "peak",
    ):
        assert kept in ents, f"{kept!r} must remain reachable as a seed"


@pytest.mark.parametrize(
    ("text", "words"),
    [
        ("next.js, socket.io and config.yaml", ["next.js", "socket.io", "and", "config.yaml"]),
        ("x.ai, t.co, ASP.NET and Node.JS", ["x.ai", "t.co", "ASP.NET", "and", "Node.JS"]),
        # A run of single letters is an abbreviation, not a name.
        ("the U.S. office", ["the", "U", "S", "office"]),
        ("tools, e.g. a linter", ["tools", "e", "g", "a", "linter"]),
        # Digits are no letters: a version stays whole.
        ("Python 3.9 and 3.11", ["Python", "3.9", "and", "3.11"]),
    ],
)
def test_a_dot_is_inside_a_word_unless_every_part_is_one_letter(
    text: str, words: list[str]
) -> None:
    from server.memory.domain import triples_follow_rows as tfr

    assert tfr.split_words(text, dots=True) == words


@pytest.mark.parametrize(
    ("raw", "canonical"),
    [
        ("दिल्ली", "दिल्ली"),
        ("กรุงเทพ", "กรุงเทพ"),
        # Typed as one composed letter or as a letter and its mark: one node.
        ("Zu\u0308rich", "zürich"),
        ("Zürich", "zürich"),
        # "İ".lower() is "i" and a dot above, which nobody types.
        ("İstanbul", "istanbul"),
        ("Istanbul", "istanbul"),
        # Latin and ASCII are as they were.
        ("The Straße", "straße"),
        ("Café-Bar!", "café-bar"),
        ("node.js", "node.js"),
        ("Alice's   API responses", "alice api response"),
        # A compatibility spelling is the same name: full-width letters, a ligature.
        ("ＴＯＫＹＯ", "tokyo"),
        ("Tokyo", "tokyo"),
        ("ﬁnance", "finance"),
        # A mark with no letter before it marks nothing.
        ("\u0301Tokyo", "tokyo"),
        ('"\u0301Tokyo"', "tokyo"),
    ],
)
def test_the_canonical_form_keeps_combining_marks(raw: str, canonical: str) -> None:
    assert canonical_entity_text(raw) == canonical
    assert canonical_entity_text(canonical) == canonical


@pytest.mark.parametrize(
    ("text", "names"),
    [
        ("Иван lives in Москва", ["иван", "москва"]),
        ("محمد lives in القاهرة", ["محمد", "القاهرة"]),
        ("田中 lives in 東京", ["田中", "東京"]),
        ("김민준 lives in 서울", ["김민준", "서울"]),
        ("I live in Zürich", ["zürich"]),
        # Each word stays whole, vowel signs and all.
        ("मैं नई दिल्ली में रहता हूँ", ["दिल्ली"]),
        ("मैं दिल्ली में रहता हूँ", ["दिल्ली", "रहता"]),
        ("ฉันอยู่ กรุงเทพ", ["กรุงเทพ"]),
        ("I live in İstanbul", ["istanbul"]),
        ("I live in Zu\u0308rich", ["zürich"]),
        # A run of unspaced text is not a name.
        ("東京に住んでいます", []),
        ("Lives in Αθήνας", ["αθήνας"]),
        # A dot between letters or digits is inside the name.
        (
            "we deploy with next.js and socket.io on k8s, see config.yaml",
            ["next.js", "socket.io", "k8s", "config.yaml"],
        ),
    ],
)
def test_a_name_in_any_script_is_an_entity(text: str, names: list[str]) -> None:
    ents = _candidate_entities_from_value(text)
    assert all(n in ents for n in names), ents
    assert "rich" not in ents
    assert "東京に住んでいます" not in ents
    # No fragment of a word whose vowel signs became spaces.
    assert not {"द ल ल", "रहत", "ฉ นอย", "กร งเทพ", "i stanbul", "zu rich"} & set(ents)
    assert not {"next", "socket", "config", "yaml"} & set(ents)


@pytest.mark.parametrize("text", ["我住在北京", "北京是首都", "我们明天去上海"])
def test_a_spaceless_chinese_sentence_is_not_one_entity(text: str) -> None:
    assert text not in _candidate_entities_from_value(text)


@pytest.mark.parametrize("name", ["东京", "毛泽东", "乌鲁木齐"])
def test_a_chinese_name_of_up_to_four_characters_is_an_entity(name: str) -> None:
    assert name in _candidate_entities_from_value(f"We met in {name} today")


# --- batched embedding order --------------------------------------------------


@pytest.mark.asyncio
async def test_embed_many_maps_results_by_index_not_position() -> None:
    """A reordered provider response must not swap vectors.

    `memory.set` batches [value, memory_key]. A swap puts the key vector in the
    value column, which corrupts search silently — no error, no bad row, just
    wrong distances forever.
    """
    from server.embedding import openai as oai

    emb = oai.OpenAIEmbedder.__new__(oai.OpenAIEmbedder)
    emb.model_name = "text-embedding-3-small"  # type: ignore[misc]
    emb.dim = 0  # type: ignore[misc]
    emb._api_key = "sk-test"  # type: ignore[misc]
    emb._endpoint = "https://example.invalid/v1/embeddings"  # type: ignore[misc]
    emb._encoder = "approx"  # type: ignore[misc]

    class _Resp:
        status_code = 200

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            # Deliberately reversed: index 1 first.
            return {
                "usage": {"total_tokens": 4},
                "data": [
                    {"index": 1, "embedding": [9.0, 9.0]},
                    {"index": 0, "embedding": [1.0, 1.0]},
                ],
            }

    class _Client:
        async def post(self, *a, **k):
            return _Resp()

    import server.embedding.openai as mod

    orig = mod._http_client
    mod._http_client = lambda: _Client()  # type: ignore[assignment]
    try:
        vecs, _billed, _hits = await emb._embed_many_raw(["value-text", "key-text"])
    finally:
        mod._http_client = orig  # type: ignore[assignment]

    assert vecs[0] == [1.0, 1.0], "input 0 must get the entry whose index is 0"
    assert vecs[1] == [9.0, 9.0], "input 1 must get the entry whose index is 1"


@pytest.mark.asyncio
async def test_embed_many_refuses_a_short_response() -> None:
    """Fewer vectors than inputs must raise, not leave a slot unfilled."""
    from server.embedding import openai as oai

    emb = oai.OpenAIEmbedder.__new__(oai.OpenAIEmbedder)
    emb.model_name = "text-embedding-3-small"  # type: ignore[misc]
    emb.dim = 0  # type: ignore[misc]
    emb._api_key = "sk-test"  # type: ignore[misc]
    emb._endpoint = "https://example.invalid/v1/embeddings"  # type: ignore[misc]
    emb._encoder = "approx"  # type: ignore[misc]

    class _Resp:
        status_code = 200

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {"usage": {}, "data": [{"index": 0, "embedding": [1.0]}]}

    class _Client:
        async def post(self, *a, **k):
            return _Resp()

    import server.embedding.openai as mod

    orig = mod._http_client
    mod._http_client = lambda: _Client()  # type: ignore[assignment]
    try:
        with pytest.raises(RuntimeError, match="batch size mismatch"):
            await emb._embed_many_raw(["a", "b"])
    finally:
        mod._http_client = orig  # type: ignore[assignment]


# --- provider prompt-cache accounting ------------------------------------------


def _gen_span():
    """A _Generation bound to a recording fake span."""
    from server.observability import genai

    class _Span:
        def __init__(self) -> None:
            self.attrs: dict = {}

        def set_attribute(self, k, v) -> None:
            self.attrs[k] = v

    g = genai._Generation.__new__(genai._Generation)
    span = _Span()
    g._span = span  # type: ignore[attr-defined]
    g._model = "gpt-4o-mini"  # type: ignore[attr-defined]
    g._attempts = 0  # type: ignore[attr-defined]
    return g, span


def test_prompt_cache_read_is_recorded_with_a_ratio() -> None:
    """loom's hottest LLM call ships a ~15k-token static system prompt, so the
    provider cache hit rate is the main lever on its latency and cost. Without
    it on the trace, calls with identical token counts and very different
    latencies cannot be explained."""
    g, span = _gen_span()
    g.record_usage(16000, 200, priced=True, cached_input_tokens=15000)
    assert span.attrs["gen_ai.usage.input_tokens"] == 16000
    assert span.attrs["gen_ai.usage.cached_input_tokens"] == 15000
    assert span.attrs["loom.llm.prompt_cache_hit_ratio"] == 0.9375


def test_cached_tokens_do_not_alter_reported_input_tokens() -> None:
    """Reported alongside, never subtracted: the provider's `prompt_tokens` is
    the billed total, and making the two disagree would put loom's token
    figure out of step with the invoice. The DOLLARS price the cached share
    at the provider's cache rate (0.5x on OpenAI), which is
    what the invoice does."""
    g, span = _gen_span()
    g.record_usage(16000, 200, priced=True, cached_input_tokens=15000)
    from server.observability.cost_accumulator import llm_cost_usd, turn_cost_usd

    assert span.attrs["gen_ai.usage.input_tokens"] == 16000
    assert span.attrs["gen_ai.usage.cost"] == pytest.approx(
        turn_cost_usd("gpt-4o-mini", input_tokens=1000, output_tokens=200, cache_read_tokens=15000)
    )
    assert span.attrs["gen_ai.usage.cost"] < llm_cost_usd("gpt-4o-mini", 16000, 200)


def test_no_cache_attributes_when_nothing_was_cached() -> None:
    """A zero must be absent, not present-and-zero: 'no cache read' and 'cache
    read of nothing' are the same event, and an explicit 0 invites a panel to
    average it in."""
    g, span = _gen_span()
    g.record_usage(500, 50, priced=True)
    assert "gen_ai.usage.cached_input_tokens" not in span.attrs
    assert "loom.llm.prompt_cache_hit_ratio" not in span.attrs


# --- entity pair budget fairness -----------------------------------------------


def _pair_degrees(n: int, budget: int, *, offset_major: bool) -> dict[int, int]:
    """Per-entity edge count under a truncated pair enumeration."""
    deg = dict.fromkeys(range(n), 0)
    count = 0
    if offset_major:
        for offset in range(1, n):
            if count >= budget:
                break
            for i in range(n - offset):
                if count >= budget:
                    break
                deg[i] += 1
                deg[i + offset] += 1
                count += 1
    else:
        for i in range(n):
            if count >= budget:
                break
            for j in range(i + 1, n):
                if count >= budget:
                    break
                deg[i] += 1
                deg[j] += 1
                count += 1
    return deg


def test_pair_budget_is_spent_evenly_across_entities() -> None:
    """`_MAX_PAIRS_PER_MEMORY` (100) truncates whenever there are more than 14
    candidates, and `_SET_ENTITY_MAX_CANDIDATES` is 24 — so truncation is the
    normal case, not the edge case. Because `ents` is sorted, i-major
    enumeration made a node's co-occurrence weight depend on its spelling."""
    n, budget = 24, 100
    old = _pair_degrees(n, budget, offset_major=False)
    new = _pair_degrees(n, budget, offset_major=True)

    old_skew = max(old.values()) / min(old.values())
    new_skew = max(new.values()) / min(new.values())

    # The defect: the alphabetically-first four entities take 23 edges each
    # while the last five get 4 — a 5.75x spread decided by spelling.
    assert max(old.values()) == 23 and min(old.values()) == 4
    assert old_skew > 5.0

    # The offset walk more than halves it: a smooth 4 -> 10 gradient whose extremes are
    # the FIRST and LAST entity. Deliberately not claiming equalization — offset
    # enumeration still leaves a boundary effect, because ents[0] and ents[-1]
    # participate in each offset from one side only. The point is that the
    # remaining skew is positional and symmetric rather than alphabetical.
    assert max(new.values()) == 10
    assert new_skew <= 2.6
    assert new_skew < old_skew / 2


def test_offset_enumeration_visits_every_pair_exactly_once() -> None:
    """Fairness must not come at the cost of correctness: with no truncation the
    two orders must produce the identical pair SET."""
    n = 9
    want = {(i, j) for i in range(n) for j in range(i + 1, n)}
    got = set()
    for offset in range(1, n):
        for i in range(n - offset):
            got.add((i, i + offset))
    assert got == want
    assert len(got) == n * (n - 1) // 2


def test_canonical_order_survives_offset_enumeration() -> None:
    """`entity_a <= entity_b` is relied on by the bidirectional read query, and
    offset enumeration must preserve it — it does, because `ents` is sorted."""
    ents = sorted(["zulu", "alpha", "mike", "bravo", "yankee"])
    for offset in range(1, len(ents)):
        for i in range(len(ents) - offset):
            assert ents[i] <= ents[i + offset]


@pytest.mark.parametrize(
    ("text", "junk"),
    [
        # A polite Korean verb: "I live in Seoul".
        ("서울 에 살아요", ["살아요"]),
        ("저는 학생 입니다", ["입니다"]),
        # Listed greetings, copulas and verb endings written alone are not names.
        ("こんにちは です ます", ["こんにちは", "です", "ます"]),
        ("東京 に いる する ある", ["いる", "する", "ある"]),
        ("東京 に 行きました", ["行きました"]),
        # Common words in nearly every sentence: "please", "thank you", "and", "very",
        # "went".
        ("東京 に 行った", ["行った"]),
        ("ください ありがとう そして とても", ["ください", "ありがとう", "そして", "とても"]),
    ],
)
def test_a_spaced_korean_or_japanese_verb_is_not_an_entity(text: str, junk: list[str]) -> None:
    ents = _candidate_entities_from_value(text)
    assert not {canonical_entity_text(j) for j in junk} & set(ents), ents
    for name in ("서울", "東京"):
        if name in text:
            assert canonical_entity_text(name) in ents


@pytest.mark.parametrize(
    "text",
    ["さいたま に 住んで いる", "田中 lives in 東京", "さいたま と なごや"],
)
def test_a_japanese_name_written_in_hiragana_is_an_entity(text: str) -> None:
    # Saitama is written in hiragana; the script alone says nothing about grammar.
    ents = _candidate_entities_from_value(text)
    for name in ("さいたま", "なごや", "田中", "東京"):
        if name in text:
            assert canonical_entity_text(name) in ents, ents
    assert canonical_entity_text("いる") not in ents


@pytest.mark.parametrize(
    ("text", "names", "junk"),
    [
        # "I lived in Seoul": a particle on the noun, a plain past-tense verb.
        ("저는 서울에 살았다", ["서울"], ["저는", "서울에", "살았다", "저"]),
        ("서울을 좋아한다", ["서울"], ["서울을", "좋아한다"]),
        ("김민준 은 서울 이 좋다", ["김민준", "서울"], ["좋다"]),
        ("학생 이다 그리고 조용 하다", ["학생"], ["이다", "하다"]),
        ("있다 없다 했다 간다", [], ["있다", "없다", "했다", "간다"]),
        # Plain past and future endings, and common words: "ate", "was", "will go",
        # "same", "many", "and", "really", "too", "go", "come", "see".
        (
            "먹었다 공부했었다 학생이였다 가겠다",
            [],
            ["먹었다", "공부했었다", "학생이였다", "가겠다"],
        ),
        ("서울 은 좋았다", ["서울"], ["좋았다"]),
        ("같다 많다 그리고 정말 너무", [], ["같다", "많다", "그리고", "정말", "너무"]),
        ("부산 에 가요 와요 봐요", ["부산"], ["가요", "와요", "봐요"]),
        # Names and nouns that end like a particle or a verb stay whole: a name ending
        # in 은 or 이, a province in 도, a village (마을), the sea and Canada (다).
        (
            "김하은 고양이 제주도 마을 바다 캐나다",
            ["김하은", "고양이", "제주도", "마을", "바다", "캐나다"],
            [],
        ),
        # Countries that end like a verb (간다, 완다), and one with its particle.
        ("우간다 르완다 캐나다", ["우간다", "르완다", "캐나다"], []),
        ("우간다를 좋아한다", ["우간다"], ["좋아한다"]),
        # A name ending in 요 is not a polite verb.
        ("히데요 는 서울 에 살아요", ["히데요", "서울"], ["살아요"]),
        # The particle 에서 and the plain copula come off the noun; a name keeps them.
        ("서울에서 부산에서", ["서울", "부산"], ["서울에서", "부산에서"]),
        ("학생이다 서울입니다", ["학생", "서울"], ["학생이다", "서울입니다", "이다", "입니다"]),
        ("아이다", ["아이다"], []),
    ],
)
def test_a_korean_particle_or_plain_verb_is_not_an_entity(
    text: str, names: list[str], junk: list[str]
) -> None:
    ents = _candidate_entities_from_value(text)
    assert all(canonical_entity_text(n) in ents for n in names), ents
    assert not {canonical_entity_text(j) for j in junk} & set(ents), ents


@pytest.mark.parametrize(
    ("text", "names", "junk"),
    [
        # "We go to school", "She likes Busan now": a pronoun or a time word, once its
        # particle is off, is in nearly every sentence and names nothing.
        ("우리는 학교에 간다", ["학교"], ["우리"]),
        ("그녀는 지금 부산을 좋아해요", ["부산"], ["그녀", "지금"]),
        ("저희는 오늘 서울에 가요", ["서울"], ["저희", "오늘"]),
        ("당신을 어제 만났다", [], ["당신", "어제"]),
        ("너희를 내일 그들을 본다", [], ["너희", "내일", "그들"]),
        # A time word with its 은 or 는.
        ("오늘은 지금은 내일은 어제는 부산", ["부산"], ["오늘은", "지금은", "내일은", "어제는"]),
    ],
)
def test_a_korean_pronoun_or_time_word_is_not_an_entity(
    text: str, names: list[str], junk: list[str]
) -> None:
    ents = _candidate_entities_from_value(text)
    assert all(canonical_entity_text(n) in ents for n in names), ents
    assert not {canonical_entity_text(j) for j in junk} & set(ents), ents


def test_text_longer_than_a_name_is_no_entity() -> None:
    # A read binds names as query parameters, which ClickHouse caps at 128 KiB each.
    from server.memory.app.set_entities import declared_entities
    from server.memory.domain.graph_quality import MAX_ENTITY_CHARS

    longest = "Rome" + "x" * (MAX_ENTITY_CHARS - 4)
    huge = "Zed" + "x" * 200_000
    assert canonical_entity_text(longest) == longest.lower()
    assert canonical_entity_text(longest + "x") == ""
    assert declared_entities([huge, "Bob Lane"]) == ["bob lane"]
    mined = _candidate_entities_from_value(f"{huge} met Bob Lane")
    assert "bob lane" in mined and max(map(len, mined)) <= MAX_ENTITY_CHARS


def test_an_entity_filter_of_only_long_names_falls_back_to_the_parsed_entities() -> None:
    from server.memory.api.models_search import MemorySearchInput
    from server.memory.app.search.scope import _entity_filter
    from server.memory.domain.graph_quality import MAX_ENTITY_CHARS

    long = "x" * (MAX_ENTITY_CHARS + 1)
    own = MemorySearchInput(org="o", namespace="n", query="q", entity_filter=[long, "Ann"])
    assert _entity_filter(own, ["Bob"]) == ["Ann"]
    only_long = MemorySearchInput(org="o", namespace="n", query="q", entity_filter=[long])
    assert _entity_filter(only_long, ["Bob", long]) == ["Bob"]
