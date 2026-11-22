"""Context signatures belong to the chunk's definition, including same-named accessors."""

from __future__ import annotations

from server.code.app.index.ingest_chunks import chunk_context_texts
from server.code.domain.extract.chunker import Chunk
from server.code.domain.extract.treesitter import ParsedFile, Symbol


def _symbol(signature: str, *, scope: str = "project::Widget::value") -> Symbol:
    return Symbol(
        qualified_name=scope,
        name="value",
        kind="function",
        signature=signature,
        path="widget.py",
        start_line=1,
        end_line=3,
    )


def _chunk(owner: Symbol) -> Chunk:
    return Chunk(
        path=owner.path,
        scope=owner.qualified_name,
        kind="function",
        start_line=owner.start_line,
        end_line=owner.end_line,
        content="def value(self):\n    return self._value\n",
        symbol_ids=[owner.id],
    )


def _contexts(symbols: list[Symbol], chunks: list[Chunk]) -> list[str]:
    parsed = ParsedFile(path="widget.py", lang="python", symbols=symbols)
    return chunk_context_texts("code", parsed.path, parsed, chunks, chunks, {}, {})[1]


def test_setter_changes_do_not_change_the_getters_context() -> None:
    getter = _symbol("(self)")
    before, after = _symbol("(self, value: str)"), _symbol("(self, value: str | None)")
    chunks = [_chunk(getter)]
    original = _contexts([getter, before], chunks)
    assert original == _contexts([getter, after], chunks)
    assert original == _contexts([after, getter], chunks)
    assert "project::Widget::value(self)\n---\n" in original[0]


def test_each_overload_uses_its_own_signature_independent_of_insert_order() -> None:
    integer, text = _symbol("(int value)"), _symbol("(str value)")
    texts = _contexts([integer, text], [_chunk(text), _chunk(integer)])
    assert "project::Widget::value(str value)\n---\n" in texts[0]
    assert "project::Widget::value(int value)\n---\n" in texts[1]


def test_class_chunks_do_not_borrow_member_signatures() -> None:
    member = _symbol("(int value)")
    chunk = Chunk(
        path="widget.py",
        scope="project::Widget",
        kind="class_skeleton",
        start_line=1,
        end_line=4,
        content="class Widget: ...",
        symbol_ids=["class", member.id],
    )
    assert "project::Widget\n---\n" in _contexts([member], [chunk])[0]


def test_an_absent_owner_does_not_borrow_a_same_named_definition() -> None:
    getter, setter = _symbol("(self)"), _symbol("(self, value)")
    text = _contexts([setter], [_chunk(getter)])[0]
    assert "project::Widget::value\n---\n" in text
