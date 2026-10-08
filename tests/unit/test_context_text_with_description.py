"""A backfilled description lands in the text ingest embeds, every other head line kept."""

from __future__ import annotations

from server.code.domain.extract.context_text import build_context_text, with_description


def _text(description: str = "") -> str:
    return build_context_text(
        path="src/Interpreters/Queue.cpp",
        scope="DB::Queue::push",
        signature="(Entry e)",
        description=description,
        content="void push(Entry e) {}\n",
        calls=["DB::Queue::flush"],
        callers=["DB::Handler::run"],
    )


def test_a_structural_text_gets_the_description_ingest_would_have_written() -> None:
    assert with_description(
        _text(), scope="DB::Queue::push", description="Queues one entry."
    ) == _text("Queues one entry.")


def test_an_existing_description_is_replaced_not_stacked() -> None:
    got = with_description(_text("Old."), scope="DB::Queue::push", description="New.")
    assert got == _text("New.")


def test_a_text_without_the_content_separator_is_left_alone() -> None:
    assert with_description("no separator", scope="s", description="d") == "no separator"
