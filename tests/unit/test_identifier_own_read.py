"""An exact identifier gets its own read."""

from __future__ import annotations

from server.memory.app.ask.identifiers import _identifier_groups, _injectable_identifiers


def test_product_words_are_not_injectable_identifiers() -> None:
    idents = _injectable_identifiers(
        "What do we know about GitHub issue ClickHouse 107091 and its CI failure?"
    )
    assert idents == ["107091"]


def test_salient_identifiers_form_their_own_group() -> None:
    assert _identifier_groups(["107091", "GitHub", "@rdev1", "toFixedString"]) == [
        ["107091", "@rdev1"],
        ["GitHub", "toFixedString"],
    ]
    assert _identifier_groups(["toFixedString"]) == [["toFixedString"]]
    assert _identifier_groups([]) == []
