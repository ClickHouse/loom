"""The triple plane follows the row plane's supersession. Pure planning
against the MemoryAgentBench shapes (stale open triple, update row active
under another key)."""

from __future__ import annotations

import pytest

from server.memory.domain import triples_follow_rows as tfr
from server.memory.domain.conflicts import intra_batch_supersessions


def test_entity_in_tolerates_backfill_stems() -> None:
    text = "The chairperson of Fatah is Mahmoud Abbas."
    assert tfr.entity_in(text, "mahmoud abba")  # the graph's stem
    assert tfr.entity_in(text, "fatah")
    assert not tfr.entity_in(text, "moshe kahlon")
    assert tfr.entity_in("The director of The Beatles is Joe Hockey.", "beatle")
    assert not tfr.entity_in("anything", "")


def test_substituted_spans_finds_the_one_differing_span() -> None:
    assert tfr.substituted_spans(
        "The chairperson of Fatah is Mahmoud Abbas.", "The chairperson of Fatah is Moshe Kahlon."
    ) == ("mahmoud abbas", "moshe kahlon")
    assert tfr.substituted_spans(
        "Basketball was created in the country of United States of America.",
        "Basketball was created in the country of Soviet Union.",
    ) == ("united states of america", "soviet union")
    # a subject swap is a span too
    assert tfr.substituted_spans("Steve Sax likes baseball.", "Bo Ryan likes baseball.") == (
        "steve sax",
        "bo ryan",
    )
    # the sweep's lead-in is not part of the shape
    assert tfr.substituted_spans(
        "Thomas Arne was born in the city of London.",
        "User stated that Thomas Arne was born in the city of Bengaluru.",
    ) == ("london", "bengaluru")
    # not the same shape: nothing to copy
    assert tfr.substituted_spans("Alice moved to Paris.", "Bob likes tea a lot.") is None
    assert tfr.substituted_spans("same text", "same text") is None
    assert tfr.substituted_spans("Alice", "Alice moved") is None


def test_plan_closes_the_stale_triple_and_writes_the_successor() -> None:
    open_triples = [
        {
            "subject": "fatah",
            "relation": "chairperson_of",
            "object": "mahmoud abba",
            "confidence": 0.9,
            "provenance": "extracted",
        },
        {"subject": "mahmoud abba", "relation": "profession", "object": "politician"},
        {"subject": "steve sax", "relation": "associ_with_sport", "object": "baseball"},
    ]
    pairs = [
        tfr.RowSupersession(
            "The chairperson of Fatah is Mahmoud Abbas.",
            "The chairperson of Fatah is Moshe Kahlon.",
            winner_key="moshe_kahlon_chairperson",
        ),
        tfr.RowSupersession(
            "Steve Sax is associated with the sport of baseball.",
            "Steve Sax is associated with the sport of association football.",
            winner_key="steve_sax_association",
        ),
    ]
    plan = tfr.plan_follow(open_triples, pairs)
    assert plan.closes == [
        ("fatah", "chairperson_of", "mahmoud abba"),
        ("steve sax", "associ_with_sport", "baseball"),
    ]
    # Abbas's own profession is not the superseded row's business
    assert ("mahmoud abba", "profession", "politician") not in plan.closes
    assert [(*i.triple, i.memory_key) for i in plan.inserts] == [
        ("fatah", "chairperson_of", "moshe kahlon", "moshe_kahlon_chairperson"),
        ("steve sax", "associ_with_sport", "association football", "steve_sax_association"),
    ]
    assert plan.inserts[0][5] == 0.9  # confidence copied from the closed triple
    assert plan.inserts[0][6] == "extracted"  # and its provenance
    assert plan.inserts[1][6] == ""  # a predecessor without one passes none on


def test_plan_keeps_triples_the_successor_still_attests_and_lists_open_successors() -> None:
    open_triples = [
        {"subject": "amala paul", "relation": "citizen_of", "object": "india"},
        {"subject": "amala paul", "relation": "citizen_of", "object": "belgium"},
    ]
    # an elaboration keeps the value: nothing closes
    plan = tfr.plan_follow(
        open_triples[:1],
        [
            tfr.RowSupersession(
                "Amala Paul is a citizen of India.",
                "Amala Paul, an actress, is a citizen of India.",
            )
        ],
    )
    assert plan.closes == [] and plan.inserts == []
    # a successor triple already open
    plan = tfr.plan_follow(
        open_triples,
        [
            tfr.RowSupersession(
                "Amala Paul is a citizen of India.", "Amala Paul is a citizen of Belgium."
            )
        ],
    )
    assert plan.closes == [("amala paul", "citizen_of", "india")]
    # the winner names it as a source; the writer does not write it twice
    assert [i.triple for i in plan.inserts] == [("amala paul", "citizen_of", "belgium")]


def test_plan_closes_without_a_successor_when_the_shape_differs() -> None:
    plan = tfr.plan_follow(
        [{"subject": "thomas arne", "relation": "born_in", "object": "london"}],
        [
            tfr.RowSupersession(
                "Thomas Arne was born in the city of London.",
                "Thomas Arne: born and raised in Bengaluru, Karnataka.",
            )
        ],
    )
    assert plan.closes == [("thomas arne", "born_in", "london")]
    assert plan.inserts == []


def test_a_substituted_object_is_followed_only_by_the_fact_that_named_it() -> None:
    lives, works = ("user", "live_in", "london"), ("user", "work_in", "london")
    plan = tfr.plan_follow(
        [{"subject": s, "relation": r, "object": o} for s, r, o in (lives, works)],
        [
            tfr.RowSupersession(
                "I live in London and work in London",
                "I live in Rome and work in London",
                winner_key="k",
                loser_triples=frozenset({lives, works}),
                winner_triples=frozenset({("user", "live_in", "rome")}),
            )
        ],
    )
    # The work is still in London: it stays open, and no move to Rome is written for it.
    assert plan.closes == [lives]
    assert [i.triple for i in plan.inserts] == [("user", "live_in", "rome")]


def test_a_clause_the_update_changed_no_longer_says_the_triple() -> None:
    works = ("user", "work_in", "london")
    plan = tfr.plan_follow(
        [{"subject": "user", "relation": "work_in", "object": "london"}],
        [
            tfr.RowSupersession(
                "I work in London",
                "I no longer work in London",
                winner_key="k",
                loser_triples=frozenset({works}),
                winner_triples=frozenset({("user", "work_in", "rome")}),
            )
        ],
    )
    assert plan.closes == [works]


def test_object_substitution_requires_a_shared_subject_and_predicate() -> None:
    """A one-span rule would pair two subjects sharing an object inside one
    write and supersede the update row itself."""
    assert tfr.object_substitution(
        "The capital of Soviet Union is Moscow.", "The capital of Soviet Union is Russellville."
    ) == ("moscow", "russellville")
    assert tfr.object_substitution(
        "Basketball was created in the country of United States of America.",
        "Basketball was created in the country of Soviet Union.",
    ) == ("united states of america", "soviet union")
    assert tfr.object_substitution(
        "Moshe Kahlon works in the field of politician.",
        "User stated that Moshe Kahlon works in the field of soldier.",
    ) == ("politician", "soldier")
    # two subjects, one object: not an update
    assert (
        tfr.object_substitution(
            "Basketball was created in the country of Soviet Union.",
            "Club Nouveau was created in the country of Soviet Union.",
        )
        is None
    )
    assert (
        tfr.object_substitution(
            "The capital of Great Britain is London.", "The capital of United Kingdom is London."
        )
        is None
    )
    # a shared beginning with a different subject inside the span
    assert (
        tfr.object_substitution(
            "The capital of Soviet Union is Moscow.", "The capital of Italy is Rome."
        )
        is None
    )
    assert tfr.object_substitution("Steve Sax likes baseball.", "Bo Ryan likes baseball.") is None
    # one subject and object, two predicates: another fact, not an update
    assert (
        tfr.object_substitution(
            "The user currently lives in London.", "The user currently works in London."
        )
        is None
    )
    assert tfr.object_substitution("Bob lives in London", "Bob works in London") is None


@pytest.mark.parametrize(
    ("old", "new", "spans"),
    [
        # A shared ending after a preposition is a detail both keep, not the object.
        ("I work at Acme in London", "I work at Globex in London", ("acme", "globex")),
        ("I live in London with my wife", "I live in Paris with my wife", ("london", "paris")),
        ("I moved to Paris for work", "I moved to Berlin for work", ("paris", "berlin")),
        ("The meeting is at 3pm in Room A", "The meeting is at 4pm in Room A", ("3pm", "4pm")),
        # Terse English.
        ("Alice strongly prefers Python", "Alice strongly prefers Rust", ("python", "rust")),
        (
            "User Alice prefers dark mode",
            "User Alice prefers light mode",
            ("dark mode", "light mode"),
        ),
        ("Project Loom uses ClickHouse 24", "Project Loom uses ClickHouse 25", ("24", "25")),
        # An object an article introduces keeps its shared head words.
        (
            "The user works as a senior backend engineer",
            "The user works as a junior backend engineer",
            ("senior backend engineer", "junior backend engineer"),
        ),
        (
            "The user drives a red sports car",
            "The user drives a blue sports car",
            ("red sports car", "blue sports car"),
        ),
        (
            "The user uses the dark color theme",
            "The user uses the light color theme",
            ("dark color theme", "light color theme"),
        ),
        # Other languages.
        (
            "Mein Bruder wohnt jetzt in München",
            "Mein Bruder wohnt jetzt in Köln",
            ("münchen", "köln"),
        ),
        ("Il utente vive a Roma", "Il utente vive a Milano", ("roma", "milano")),
        ("Президент компании Акме Боб", "Президент компании Акме Ян", ("боб", "ян")),
    ],
)
def test_an_update_of_the_object_pairs(old, new, spans) -> None:
    assert tfr.object_substitution(old, new) == spans


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("Wang Xiaoming est âgé de 28 ans", "Wang Xiaoming est ingénieur logiciel"),
        ("Wang Xiaoming is 28 years old", "Wang Xiaoming is a software engineer"),
        ("У меня есть собака", "У меня есть кошка"),
    ],
)
def test_two_attributes_after_a_bare_name_never_pair(old, new) -> None:
    assert tfr.object_substitution(old, new) is None


@pytest.mark.parametrize(
    ("old", "new"),
    [
        # Two predicates before one shared object, in languages whose prepositions
        # the rule does not list: another fact, not an update.
        ("Мой старший брат живёт в Москве", "Мой старший брат работает в Москве"),
        ("Mon frère aîné habite à Paris", "Mon frère aîné travaille à Paris"),
        ("Mi hermano mayor vive en Madrid", "Mi hermano mayor trabaja en Madrid"),
    ],
)
def test_two_predicates_before_a_shared_ending_never_pair(old, new) -> None:
    assert tfr.object_substitution(old, new) is None
    assert intra_batch_supersessions([old, new]) == []


def test_a_span_the_detail_cut_empties_is_no_substitution() -> None:
    old, new = (
        "User stated that Wang Xiaoming is in Beijing",
        "Wang Xiaoming is currently in Beijing",
    )
    assert tfr.object_substitution(old, new) is None
    assert intra_batch_supersessions([old, new]) == []


def test_each_closed_triple_takes_its_own_successors_date() -> None:
    from datetime import datetime

    from server.memory.domain.triples_follow_rows import RowSupersession, plan_follow

    pairs = [
        RowSupersession(
            loser_text="The capital of Fatah is Ramallah",
            winner_text="The capital of Fatah is Gaza",
            loser_valid_to=datetime(2023, 5, 1),
        ),
        RowSupersession(
            loser_text="The mayor of Paris is Hidalgo",
            winner_text="The mayor of Paris is Dati",
            loser_valid_to=datetime(2024, 9, 1),
        ),
    ]
    triples = [
        {"subject": "fatah", "relation": "capital", "object": "ramallah"},
        {"subject": "paris", "relation": "mayor", "object": "hidalgo"},
    ]
    plan = plan_follow(triples, pairs)
    assert plan.closed_at == {
        ("fatah", "capital", "ramallah"): datetime(2023, 5, 1),
        ("paris", "mayor", "hidalgo"): datetime(2024, 9, 1),
    }


def test_a_triple_closes_when_its_row_was_replaced_not_when_the_live_row_began() -> None:
    from datetime import datetime

    from server.memory.domain.triples_follow_rows import RowSupersession, plan_follow

    # Paris was replaced by London in 2023; Rome, the live end of the chain, in 2024.
    pair = RowSupersession(
        loser_text="User lives in Paris",
        winner_text="User lives in Rome",
        winner_valid_from=datetime(2024, 1, 1),
        loser_valid_to=datetime(2023, 1, 1),
    )
    plan = plan_follow([{"subject": "user", "relation": "live_in", "object": "paris"}], [pair])
    assert plan.closed_at == {("user", "live_in", "paris"): datetime(2023, 1, 1)}
    assert [i[:3] + (i[4],) for i in plan.inserts] == [
        ("user", "live_in", "rome", datetime(2024, 1, 1))
    ]


def test_a_triple_two_pairs_close_ends_at_the_later_replacement() -> None:
    from datetime import datetime

    from server.memory.domain.triples_follow_rows import RowSupersession, plan_follow

    # Paris -> "Paris near the river" (replaced 05-30) -> Berlin: the middle row still
    # stated Paris until 05-30, whichever pair comes first.
    first = RowSupersession(
        loser_text="User lives in Paris",
        winner_text="User lives in Berlin",
        loser_valid_to=datetime(2023, 5, 23),
    )
    second = RowSupersession(
        loser_text="User lives in Paris near the river",
        winner_text="User lives in Berlin",
        loser_valid_to=datetime(2023, 5, 30),
    )
    triple = [{"subject": "user", "relation": "live_in", "object": "paris"}]
    for pairs in ([first, second], [second, first]):
        plan = plan_follow(triple, pairs)
        assert plan.closed_at == {("user", "live_in", "paris"): datetime(2023, 5, 30)}
    undated = RowSupersession(loser_text="User lives in Paris", winner_text="")
    assert plan_follow(triple, [second, undated]).closed_at[("user", "live_in", "paris")] is None


def test_a_successor_one_plan_writes_is_not_closed_by_another() -> None:
    from server.memory.domain.triples_follow_rows import FollowPlan, Successor, merge_plans

    berlin = ("user", "live_in", "berlin germany")
    acme = ("user", "work_at", "acme")
    writes = FollowPlan(
        closes=[("user", "live_in", "paris")],
        inserts=[Successor(*berlin, "user_city", None, 1.0, "")],
        kept={acme},
    )
    closes = FollowPlan(closes=[berlin], inserts=[], closed_at={berlin: None})
    for plans in ([writes, closes], [closes, writes]):
        merged = merge_plans(plans)
        assert merged.closes == [("user", "live_in", "paris")]
        assert [s.triple for s in merged.inserts] == [berlin]
        assert merged.kept == {acme}, "only what the plans kept, not their successors"


def _open(*triples: tuple[str, str, str]) -> list[dict]:
    return [{"subject": s, "relation": r, "object": o} for s, r, o in triples]


def test_a_linked_row_closes_the_triples_it_stated_whatever_their_subject() -> None:
    lives, visited = ("user", "lives_in", "paris"), ("user", "visited", "paris")
    london = ("user", "lives_in", "london")
    pair = tfr.RowSupersession(
        loser_text="I currently live in Paris",
        winner_text="I currently live in London",
        winner_id="w",
        loser_triples=frozenset({lives}),
        winner_triples=frozenset({london}),
    )
    plan = tfr.plan_follow(_open(lives, visited, london), [pair])
    # `user` is in neither text; another row's visit to Paris is not this row's to close.
    assert plan.closes == [lives]
    # London is open already: the winner is linked to it as its source, not written again.
    assert [(i.triple, i.memory_id) for i in plan.inserts] == [(london, "w")]


def test_a_successor_is_linked_to_its_winner_even_one_without_links() -> None:
    lives = ("user", "lives_in", "paris")
    pair = tfr.RowSupersession(
        loser_text="I live in Paris",
        winner_text="I live in London",
        winner_id="w",
        loser_triples=frozenset({lives}),
    )
    (successor,) = tfr.plan_follow(_open(lives), [pair]).inserts
    assert successor.triple == ("user", "lives_in", "london") and successor.memory_id == "w"


def test_an_inferred_link_adds_to_what_a_row_is_read_by() -> None:
    # The winner's own facts are still read by its text: the successor is extra.
    london, acme = ("user", "lives_in", "london"), ("user", "near", "acme")
    pair = tfr.RowSupersession(
        loser_text="user lives in London near acme",
        winner_text="user lives in Rome",
        loser_inferred=frozenset({london}),
    )
    assert sorted(tfr.plan_follow(_open(london, acme), [pair]).closes) == sorted([acme, london])


def test_no_retirement_closes_a_facet_edge() -> None:
    facet = {"subject": "bot", "relation": "about", "object": "paris", "provenance": "facet"}
    pair = tfr.RowSupersession(loser_text="bot notes on paris", winner_text="bot notes on rome")
    assert tfr.plan_follow([facet], [pair]).closes == []


def test_a_triple_a_live_row_still_states_is_kept_by_the_merge() -> None:
    paris = ("user", "lives_in", "paris")
    plan = tfr.FollowPlan(closes=[paris], inserts=[], closed_at={paris: None})
    assert tfr.merge_plans([plan], kept=frozenset({paris})).closes == []
    assert tfr.merge_plans([plan]).closes == [paris]


def test_each_winner_key_is_a_source_of_a_shared_successor() -> None:
    paris, london = ("alice", "live_in", "paris"), ("alice", "live_in", "london")
    city = tfr.RowSupersession(
        "Alice lives in Paris", "Alice lives in London", winner_key="user_city", winner_id="w1"
    )
    home = tfr.RowSupersession(
        "Alice home is Paris", "Alice home is London", winner_key="home", winner_id="w2"
    )
    keys = [(i.triple, i.memory_key) for i in tfr.plan_follow(_open(paris), [city, home]).inserts]
    assert keys == [(london, "user_city"), (london, "home")]
    # The same across chunks.
    plans = [tfr.plan_follow(_open(paris), [pair]) for pair in (city, home)]
    merged = tfr.merge_plans(plans).inserts
    assert [(i.triple, i.memory_key) for i in merged] == [(london, "user_city"), (london, "home")]


def test_a_linked_winner_keeps_only_what_its_links_state() -> None:
    # It mentions Paris, but as a visit: the old preference closes.
    prefers = ("user", "prefer_city", "paris")
    pair = tfr.RowSupersession(
        loser_text="The user's favorite city is Paris",
        winner_text="The user's favorite city is London, and the user visited Paris",
        winner_id="w",
        loser_triples=frozenset({prefers}),
        winner_triples=frozenset({("user", "prefer_city", "london"), ("user", "visited", "paris")}),
    )
    assert tfr.plan_follow(_open(prefers), [pair]).closes == [prefers]


def test_a_revised_value_still_states_a_successor_only_with_both_ends() -> None:
    ceo = ("bob", "ceo_of", "acme")
    assert tfr.still_states("Bob is the CEO of Acme, again", ceo)
    # The subject changed: Carol's row no longer holds Bob's fact.
    assert not tfr.still_states("Carol is the CEO of Acme", ceo)
    # A first-person fact never names its subject; its object is enough.
    lives = (tfr.SELF_SUBJECT, "lives_in", "london")
    assert tfr.still_states("I currently live in London and work at Acme", lives)
    assert not tfr.still_states("I currently live in Berlin", lives)


_LIVES = ("user", "live_in", "london")
_WAS = "I currently live in London"
_WORKS = ("user", "work_at", "acme")
_SHOPS = ("user", "shop_at", "marks and spencer")
_SISTER = ("alice", "sister_of", "bob")
_FATAH = ("fatah", "chairperson_of", "mahmoud abba")
_KENNEDY = ("user", "favourit_president", "john f kennedy")
_BOTH = "I live and work in London"


def test_a_successor_written_for_the_value_a_row_holds_always_holds() -> None:
    # The follow judged it against this value; the extractor naming it another way
    # ("reside_in") does not gainsay it.
    stated = frozenset({("user", "reside_in", "london")})
    assert tfr.carried(_WAS, "h1", stated, {_LIVES: {"h1": ""}}) == {_LIVES}


def test_a_revised_row_keeps_a_successor_only_while_it_restates_it() -> None:
    def held(now: str, stated: frozenset = frozenset()) -> bool:
        return bool(tfr.carried(now, "h2", stated, {_LIVES: {"h1": _WAS}}))

    assert held(_WAS + ".")
    # Anything added closes it, unless the extractor states it for the new value.
    assert not held(_WAS + " and work at Acme")
    assert held(_WAS + " and work at Acme", frozenset({_LIVES, _WORKS}))
    assert not held("I currently live in Rome and visited London")
    assert not held("I currently live in Berlin")
    # Restated, but gainsaid by what the extractor stated for the new value.
    assert not held(_WAS + ".", frozenset({("user", "live_in", "rome")}))
    # The value it was written for is gone (erased): nothing to judge it by.
    assert not tfr.carried(_WAS, "h2", frozenset(), {_LIVES: {"h1": ""}})


def test_a_declared_many_valued_relation_is_not_gainsaid_by_another_object() -> None:
    was = "I currently live in London and Rome"
    stated = frozenset({("user", "live_in", "rome")})
    written = {_LIVES: {"h1": was}}
    assert not tfr.carried(was + ".", "h2", stated, written)
    assert tfr.carried(was + ".", "h2", stated, written, {"live_in": "many"}) == {_LIVES}


def test_another_relation_to_the_same_object_is_another_fact() -> None:
    was = "I currently live in London and work in London"
    stated = frozenset({("user", "work_in", "london")})
    assert tfr.carried(was + ".", "h2", stated, {_LIVES: {"h1": was}}) == {_LIVES}


def test_a_name_in_any_script_is_still_stated() -> None:
    ceo = ("bob", "ceo_of", "компания")
    was = "Bob is CEO of Компания"
    assert tfr.entity_in(was, "компания")
    assert tfr.still_states(was + "!", ceo)
    assert tfr.carried(was + "!", "h2", frozenset(), {ceo: {"h1": was}}) == {ceo}
    # An update pairs whatever script its objects are in.
    for city in ("Zürich", "Москва", "Hawai\u02bbi"):
        assert tfr.object_substitution(
            f"I currently live in {city}", "I currently live in Berlin"
        ) == (city.casefold(), "berlin")
    # A vowel sign or virama is part of its word.
    assert tfr.split_words("नई दिल्ली में रहता हूँ") == ["नई", "दिल्ली", "में", "रहता", "हूँ"]
    lives = ("user", "live_in", "नई दिल्ली")
    assert tfr.still_states("मैं नई दिल्ली में रहता हूँ", lives)
    assert not tfr.still_states("मैं नल में रहता हूँ", ("user", "live_in", "नल्ली"))


_LIVE = "I live in London"
_BEATLES = ("user", "favourit_band", "beatle")
_JNJ = ("johnson and johnson", "headquarter_in", "new brunswick")


@pytest.mark.parametrize(
    ("old", "new", "triple"),
    [
        # The same words, or fewer.
        (_WAS, _WAS + ".", _LIVES),
        (_WAS, _WAS + "!", _LIVES),
        ("I live in London, not Paris", _LIVE, _LIVES),
        ("I live in London, unless I move", _LIVE, _LIVES),
        ("I live in London. I work at Acme.", _LIVE + ".", _LIVES),
        ("I work at Acme. I live in London.", "I live in London.", _LIVES),
        ("Alice is Bob's sister, they say", "Alice is Bob's sister", _SISTER),
        ("Alice is Bob's sister", "Alice is Bob\u2019s sister.", _SISTER),
        ("I shop at Marks and Spencer", "I shop at Marks and Spencer.", _SHOPS),
        ("I live in Zürich", "I live in Zürich.", ("user", "live_in", "zürich")),
        ("London, where I live.", "London, where I live", _LIVES),
        ("I live in Zu\u0308rich", "I live in Z\u00fcrich.", ("user", "live_in", "zürich")),
        # A lead-in is not part of the statement.
        ("User stated that I live in London", _LIVE, _LIVES),
        # A dot inside a name or a number ends no sentence.
        ("I use next.js", "I use next.js", ("user", "use", "next.js")),
        ("I use python 3.12", "I use python 3.12.", ("user", "use", "python 3.12")),
    ],
)
def test_a_revision_that_restates_the_statement_keeps_its_successor(old, new, triple) -> None:
    assert tfr.restates(old, new, triple)


@pytest.mark.parametrize(
    ("old", "new", "triple"),
    [
        # Anything added, however harmless it looks: words cannot tell detail from a
        # retraction.
        (_WAS, "I currently live in London, near the river", _LIVES),
        (_WAS, "I currently live in London and work at Acme", _LIVES),
        ("I shop at Marks and Spencer", "I shop at Marks and Spencer, weekly", _SHOPS),
        (_BOTH, _BOTH + ", near the river", _LIVES),
        ("Alice is Bob's sister", "Alice is Bob's sister, they say", _SISTER),
        ("London", "London, UK", _LIVES),
        (
            "The chairperson of Fatah is Mahmoud Abbas",
            "The chairperson of Fatah is Mahmoud Abbas, since 2005",
            _FATAH,
        ),
        (
            "My favourite president is John F Kennedy",
            "My favourite president is John F Kennedy, still",
            _KENNEDY,
        ),
        (_LIVE, "I live in London. I work at Acme.", _LIVES),
        (_LIVE, "I live in London, until May", _LIVES),
        (_LIVE, "I live in London, then Rome", _LIVES),
        (_LIVE, "I live in London. Just kidding.", _LIVES),
        (_LIVE, "I live in London, I think", _LIVES),
        (_LIVE, "False. I live in London.", _LIVES),
        (_LIVE, "I live in London... ?", _LIVES),
        (_LIVE, "I live in London \u274c", _LIVES),
        (_LIVE, "I live in London. Неправда.", _LIVES),
        (_LIVE, "I live in London. 不是。", _LIVES),
        (_LIVE, 'I live in "London".', _LIVES),
        # Cut anywhere but right after the statement, a clause says something new.
        (
            "I live in London, moving out, they claimed, wrongly",
            "I live in London, moving out",
            _LIVES,
        ),
        (
            "I live in London. Leaving soon, people say, but not true.",
            "I live in London. Leaving soon.",
            _LIVES,
        ),
        (
            "I live in London, until May was the plan, but I stayed",
            "I live in London, until May was the plan",
            _LIVES,
        ),
        # Cut before the relation, only the object is left.
        ("London, where I live.", "London.", _LIVES),
        ("My home city: London, where I live", "My home city: London", _LIVES),
        # Sentences out of order, or a later statement dropped behind a retraction.
        ("I live in London. I work at Acme.", "I work at Acme. I live in London.", _LIVES),
        ("Just kidding. I live in London.", "I live in London. Just kidding.", _LIVES),
        ("I moved out. I live in London.", "I live in London. I moved out.", _LIVES),
        (
            "I live in London. Not anymore. I live in London.",
            "I live in London. Not anymore.",
            _LIVES,
        ),
        # A sign or a quote is a word too, wherever it stands.
        (_LIVE, "I live in London'", _LIVES),
        (_LIVE, "I live in London-", _LIVES),
        ("The temperature is 5.", "The temperature is -5.", ("temperature", "is", "5")),
        (_LIVE, "I live in 'London'", _LIVES),
        # Every statement must survive: the one kept may not be the one that held.
        (
            "I live in London. I lived in London as a child too.",
            "I lived in London as a child too.",
            _LIVES,
        ),
        (
            "I lived in London until 2020. I live in London again.",
            "I lived in London until 2020.",
            _LIVES,
        ),
        ("I live in London. I never lived in London.", "I never lived in London.", _LIVES),
        # A question states nothing.
        ("Do I live in London?", "Do I live in London?", _LIVES),
        ("Is it true I live in London? Yes.", "Is it true I live in London?", _LIVES),
        ("Do I live in London? I live in London.", "Do I live in London?", _LIVES),
        (
            "I live in London, not Paris",
            "I live in London, not Paris. I do not live in London.",
            _LIVES,
        ),
        # Reworded, or moved to the past.
        (_WAS, "I'm currently living in London, near the river", _LIVES),
        (_LIVE, "I lived in London until 2023; now I live in Rome", _LIVES),
        ("I work at Acme", "I worked at Acme until May, now at Initech", _WORKS),
        ("I work at Acme", "I was working at Acme until May", _WORKS),
        ("I work at Acme. London is home", "I work at Acme. London was home", _LIVES),
        (_WAS, "I currently live in London until 2023", _LIVES),
        (_LIVE, "I'd live in London if I could afford it", _LIVES),
        (_LIVE, "I'll live in London from June", _LIVES),
        # Anything before the statement, or a question made of it.
        (_WAS, "Still, I currently live in London", _LIVES),
        ("London is my home", "Still, London is my home", _LIVES),
        (_LIVE, "If I get the job, I live in London", _LIVES),
        (_LIVE, "Hypothetically, I live in London", _LIVES),
        (_LIVE, "Bob says, I live in London", _LIVES),
        (_LIVE, "False: I live in London", _LIVES),
        (_LIVE, "Until 2023, I live in London", _LIVES),
        (_LIVE, "Bob objected, the user said that I live in London", _LIVES),
        ("I live in London, not Paris", "Not true, I live in London", _LIVES),
        (_LIVE, "I live in London?", _LIVES),
        (_LIVE, "I live in London, right?", _LIVES),
        (_LIVE, "Do I live in London?", _LIVES),
        (_LIVE, "If I live in London, I will visit you", _LIVES),
        (_LIVE, "User denied that I live in London", _LIVES),
        (_WAS, "Nobody believes I currently live in London", _LIVES),
        ("The Beatles", "The Beatles tribute band", _BEATLES),
        ("I shop at Marks and Spencer", "I shop at Marks and Spencer weekly", _SHOPS),
        ("In London.", "Now in London.", _LIVES),
        # Denied, however it is spelled, anywhere in the sentence.
        (_WAS, "I no longer live in London", _LIVES),
        (_WAS, "I don\u2019t currently live in London", _LIVES),
        (_LIVE, "I dont live in London", _LIVES),
        (_LIVE, "I live in London, neednt I", _LIVES),
        (_LIVE, "I live in London, don\u2018t I", _LIVES),
        (_LIVE, "I live in London, doesn\u02bct matter", _LIVES),
        (_LIVE, "I shouldnt say I live in London", _LIVES),
        (_LIVE, "I havent said I live in London", _LIVES),
        (_LIVE, "Honestly, not that I live in London", _LIVES),
        (_LIVE, "Previously, I live in London", _LIVES),
        (_WAS, "I currently live in Rome; I used to live in London", _LIVES),
        # Taken back, corrected or hedged by what follows.
        (_LIVE, "I live in London. That is no longer true.", _LIVES),
        (_LIVE, "I live in London. That's not true anymore.", _LIVES),
        (_LIVE, "I live in London. Scratch that.", _LIVES),
        (_LIVE, "I live in London. Update: moved to Rome.", _LIVES),
        (_WAS, "I currently live in London? No, Rome", _LIVES),
        (_WAS, "I currently live in London? Not anymore", _LIVES),
        (_WAS, "I currently live in London. Not any more.", _LIVES),
        (_WAS, "I currently live in London? Not really.", _LIVES),
        (_WAS, "I currently live in London? Nope", _LIVES),
        (_WAS, "I currently live in London. Wrong.", _LIVES),
        (_WAS, "I currently live in London, not Rome", _LIVES),
        (_WAS, "I currently live in London. No doubt", _LIVES),
        (_WAS, "I currently live in London. Not far from Acme", _LIVES),
        (_LIVE, "I live in London — or did, until May", _LIVES),
        (_LIVE, "I live in London... or did", _LIVES),
        # The object kept, under another relation or another subject.
        (_WAS, "I currently live in Rome and visited London", _LIVES),
        (_BOTH, "I live in Rome and work in London", _LIVES),
        (_BOTH, "I work in London", _LIVES),
        ("I visited London once; I currently live in London", "I visited London once", _LIVES),
        ("London", "I left London", _LIVES),
        (
            "The chairperson of Fatah is Mahmoud Abbas",
            "The deputy of Fatah is Mahmoud Abbas",
            _FATAH,
        ),
        (
            "Alice, my coworker, is Bob's sister",
            "Carol, Alice's coworker, is Bob's sister",
            _SISTER,
        ),
        ("Alice: sister of Bob", "Carol: sister of Bob. Alice is nice", _SISTER),
        (
            "Johnson and Johnson is headquartered in New Brunswick",
            "Smith and Johnson is headquartered in New Brunswick",
            _JNJ,
        ),
    ],
)
def test_a_revision_that_does_not_restate_it_closes_its_successor(old, new, triple) -> None:
    assert not tfr.restates(old, new, triple)


@pytest.mark.parametrize(
    "text",
    [
        'Do I live in London?"',
        "Do I live in London?)",
        'Bob asked: do I live in London?" I said yes.',
        "Do I live in London?\u201d",
        "Do I live in London?\u00bb",
        'He asked "do I live in London?") I said yes.',
    ],
)
def test_a_question_closed_by_a_quote_or_bracket_states_nothing(text) -> None:
    assert tfr._statements(tfr._sentences(text, _LIVES), _LIVES) == {}


def test_a_follow_reads_dashed_and_bare_ids_and_skips_the_rest() -> None:
    from server.memory.app.entity_projections import _canonical_id

    dashed = "22222222-AAAA-2222-2222-222222222222"
    assert _canonical_id(dashed) == dashed.lower()
    assert _canonical_id(dashed.replace("-", "")) == dashed.lower()
    # Spellings Python reads but ClickHouse does not end the chain too.
    odd = ("{" + dashed + "}", "urn:uuid:" + dashed, "2222-2222" + dashed[9:])
    for value in ("", "-", "not-a-uuid", dashed[:-1], *odd):
        assert _canonical_id(value) is None


@pytest.mark.parametrize(
    ("text", "triple"),
    [
        ("I live in Austin, Texas", ("user", "live_in", "austin texa")),
        ("I live in St. Louis now.", ("user", "live_in", "st. louis")),
        ("I live in New York, NY, near the park", ("user", "live_in", "new york ny")),
        ("Dr. Smith works at Acme", ("dr. smith", "work_at", "acme")),
        ("I own 1,000 books", ("user", "own", "1 000")),
    ],
)
def test_a_name_with_a_comma_or_a_dot_is_one_clause(text, triple) -> None:
    # The comma in "Austin, Texas" and the dot in "St. Louis" are the name's, not a
    # clause or sentence break: the text still says the triple, and restates itself.
    assert tfr._says_in_one_clause(text, triple)
    assert tfr.restates(text, text, triple)
    assert tfr._still_says(tfr.RowSupersession(loser_text=text, winner_text=text), triple)


def test_a_dropped_facts_edge_ends_with_its_triples_unless_another_fact_is_held() -> None:
    from datetime import datetime

    now, dec = datetime(2026, 10, 1), datetime(2026, 12, 1)

    def row(obj: str, valid_to: datetime | None, relation: str = "live_in") -> dict:
        return {"subject": "user", "relation": relation, "object": obj, "valid_to": valid_to}

    gone = [("user", "live_in", "london"), ("user", "live_in", "rome")]
    rows = [row("london", datetime(2026, 9, 1)), row("rome", dec), row("rome", now)]
    # London's edge ends now, though its triple ended before: the dropped row named
    # London until now (`end_edges` keeps the pair while another row names both).
    # Rome's ends at its latest end.
    assert tfr.edge_ends(gone, frozenset(), rows, now) == [
        ("london", "user", now),
        ("rome", "user", dec),
    ]
    # Another fact of the pair a live row holds keeps the edge while it is valid.
    held = frozenset({("user", "work_in", "london")})
    work = [*rows, row("london", dec, "work_in")]
    assert tfr.edge_ends(gone, held, work, now) == [("rome", "user", dec)]
    # Not one that ended: "I worked in London until 2020" keeps no edge.
    worked = [*rows, row("london", datetime(2020, 1, 1), "work_in")]
    assert tfr.edge_ends(gone, held, worked, now) == [
        ("london", "user", now),
        ("rome", "user", dec),
    ]
    # A gone triple still open keeps its edge open too.
    assert tfr.edge_ends(gone, frozenset(), [*rows, row("rome", None)], now) == [
        ("london", "user", now)
    ]
    # Rows of triples not gone say nothing.
    assert tfr.edge_ends(gone[:1], frozenset(), rows, now) == [("london", "user", now)]
    assert tfr.edge_ends([], held, work, now) == []


def test_a_losers_facts_end_when_its_row_was_replaced_open_or_not() -> None:
    from datetime import datetime

    paris, acme = ("user", "live_in", "paris"), ("user", "work_at", "acme")
    jan, feb = datetime(2027, 1, 1), datetime(2027, 2, 1)

    def pair(until: datetime, *triples: tfr.Triple) -> tfr.RowSupersession:
        return tfr.RowSupersession(
            loser_text="I live in Paris",
            winner_text="I like Rome",
            loser_valid_to=until,
            loser_triples=frozenset(triples),
        )

    # Paris is not open (it was stated until later): it still gets the date.
    plan = tfr.plan_follow([], [pair(jan, paris)])
    assert plan.closes == [] and plan.ends == {paris: jan}
    merged = tfr.merge_plans([plan, tfr.plan_follow([], [pair(feb, paris, acme)])])
    assert merged.ends == {paris: feb, acme: feb}
