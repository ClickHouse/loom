"""The computed attribution note under the question."""

from __future__ import annotations

from types import SimpleNamespace

from server.memory.domain.ask_attribution import attribution_note, named_speaker, roster
from server.observability import event_args


def _fact(key: str, speaker: str, text: str, about_other: bool = False) -> SimpleNamespace:
    return SimpleNamespace(
        memory_key=key,
        speaker=speaker,
        content_excerpt=text,
        about_other=about_other,
        source="fact",
    )


def _chunk(text: str) -> SimpleNamespace:
    return SimpleNamespace(
        memory_key="chunk:ns:session_2:3", speaker="", content_excerpt=text, source="chunk"
    )


CHUNK = _chunk("[user] Deborah: Glad you found peace.\n[user] Jolene: My snake calms me.")


def test_fires_when_every_fact_is_the_partner_about_themselves() -> None:
    mems = [
        CHUNK,
        _fact("jolene_snake", "jolene", "Jolene finds her snake comforting."),
        _fact("jolene_yoga", "jolene", "Jolene practices yoga with her snakes."),
    ]
    note = attribution_note("How does Deborah describe the time with her snakes?", mems)
    assert note.startswith("Attribution: the question is about Deborah.")
    assert "2 fact rows on this topic are Jolene speaking about Jolene's own life" in note
    assert "unsupported unless a conversation excerpt below shows Deborah saying it" in note


def test_silent_when_the_named_speaker_said_it_or_was_spoken_about() -> None:
    mems = [CHUNK, _fact("deb_snake", "deborah", "Deborah likes snakes.")]
    assert attribution_note("How does Deborah describe her snakes?", mems) == ""
    # The partner's row ABOUT the named person is evidence about them.
    mems = [CHUNK, _fact("jolene_on_deb", "jolene", "Deborah misses her mother.", about_other=True)]
    assert attribution_note("What does Deborah miss?", mems) == ""


def test_silent_without_a_single_named_speaker_or_without_fact_rows() -> None:
    mems = [CHUNK, _fact("jolene_snake", "jolene", "Jolene finds her snake comforting.")]
    assert attribution_note("Did Deborah and Jolene meet?", mems) == ""
    assert attribution_note("Who has a snake?", mems) == ""
    assert attribution_note("What did Deborah say?", [CHUNK]) == ""
    assert attribution_note("", mems) == ""
    # Whole-word match: "Deb" is not Deborah.
    assert attribution_note("What did Deb say?", mems) == ""


def test_only_rows_on_the_questions_topic_count() -> None:
    mems = [
        CHUNK,
        _fact("jolene_snake", "jolene", "Jolene finds her snake comforting."),
        # An unrelated row by the named person must not silence the note.
        _fact("deb_job", "deborah", "Deborah works as a nurse."),
    ]
    note = attribution_note("How does Deborah describe the time with her snakes?", mems)
    assert "1 fact row on this topic is Jolene" in note
    # A topical row by the named person does silence it.
    mems.append(_fact("deb_snake", "deborah", "Deborah is scared of snakes."))
    assert attribution_note("How does Deborah describe the time with her snakes?", mems) == ""
    # A question with no topic word beyond the names never fires.
    assert attribution_note("Who is Deborah?", mems) == ""


def test_roster_reads_tags_and_chunk_lines() -> None:
    mems = [CHUNK, _fact("k", "jolene", "x")]
    assert roster(mems) == {"Deborah", "Jolene"}
    assert named_speaker("How does deborah feel?", roster(mems)) == "Deborah"
    assert named_speaker("Deborah asked Jolene", roster(mems)) == ""


def test_projected_to_the_ledger() -> None:
    assert "attribution_note" in event_args.IDENTIFIER_KEYS
