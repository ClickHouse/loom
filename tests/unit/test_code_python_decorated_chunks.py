"""Function chunks retain proven decorator ownership."""

from server.code.domain.extract.lang_python import parse_python
from server.code.domain.extract.py_decorated_chunks import python_chunks


def test_chunk_start_follows_parsed_decorator_ownership():
    source = (
        "items = (\n"
        '    """\n\n'
        "@this_is_a_long_name_printed_in_a_string(\n"
        '    # """,\n'
        ")\n"
        "def undecorated():\n"
        "    return 42\n\n"
        "@command()\n"
        "@option(\n"
        "    callback=verify,\n"
        ")\n"
        "def decorated():\n"
        "    return 42\n"
    )
    parsed = parse_python("cli.py", source.encode())
    symbols = {s.name: s for s in parsed.symbols}
    chunks = {c.scope: c for c in python_chunks(parsed, max_chars=6000) if c.kind == "function"}
    plain, decorated = symbols["undecorated"], symbols["decorated"]
    assert chunks[plain.qualified_name].start_line == plain.start_line == 7
    assert decorated.start_line == 14
    assert chunks[decorated.qualified_name].start_line == 10
    assert chunks[decorated.qualified_name].symbol_ids == [decorated.id]


def test_class_skeleton_keeps_the_decorated_method_signature():
    source = b"class Counter:\n    @property\n    def value(self):\n        return 42\n"
    parsed = parse_python("counter.py", source)
    chunks = python_chunks(parsed, max_chars=6000)
    skeleton = next(chunk for chunk in chunks if chunk.kind == "class_skeleton")
    method = next(chunk for chunk in chunks if chunk.kind == "function")
    assert "    @property\n    def value(self):" in skeleton.content
    assert method.start_line == 2 and method.end_line == 4
