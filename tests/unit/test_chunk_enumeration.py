"""An enumeration is one unit at chunking time; a sentence end beats a character cut."""

from __future__ import annotations

from server.memory.app.chunk_projection import chunk_text

ITEMS = [
    "1. Escovitch Fish - a Jamaican dish where fried snapper is topped with a spicy pickled vegetable sauce made with onions, carrots and peppers.",
    "2. Fish Curry - a Trinidadian dish that features snapper cooked in a curry sauce with vegetables and aromatics such as garlic and ginger.",
    "3. Conch & Snapper Stew - a dish from the Bahamas that combines conch and snapper in a tomato-based stew with peppers and onions.",
    "4. Grilled Snapper with Mango Salsa - a popular dish in many Caribbean countries where grilled snapper is topped with a fruity and spicy salsa.",
    "5. Brown Stew Fish - a Jamaican dish where the snapper is browned and simmered in a rich gravy with vegetables and spices.",
]


def test_a_list_that_fits_is_never_split_between_its_items() -> None:
    text = (
        "[user] Oh, I love snapper! What are some popular Caribbean dishes that feature it?\n"
        "[assistant] There are many popular Caribbean dishes that feature snapper, some of which include:\n\n"
        + "\n".join(ITEMS)
        + "\n\n[user] Wow, all of those sound amazing! Which one do you recommend I try first?"
    )
    chunks = [c for c, _, _ in chunk_text(text)]
    holders = [c for c in chunks if "Mango Salsa" in c]
    assert holders, chunks
    assert all(item in holders[0] for item in ITEMS), holders[0]


def test_a_very_long_list_still_splits() -> None:
    text = "[assistant] Options:\n" + "\n".join(
        f"{i}. Option number {i} described at some length so that the whole list is far too long to keep."
        for i in range(1, 60)
    )
    chunks = chunk_text(text)
    assert len(chunks) > 1


def test_a_character_cut_prefers_a_sentence_end() -> None:
    sentence = "One of the best hiking trails in the Natural Park of Moncayo is the GR-90. "
    filler = (
        "This trail takes you through the park's most stunning landscapes and offers panoramic views. "
        * 12
    )
    text = "[assistant] " + sentence + filler
    chunks = [c for c, _, _ in chunk_text(text)]
    assert chunks[0].rstrip().endswith((".", "?", "!"))
    assert "GR-90." in chunks[0]
