"""A definition's fingerprint covers every declaration callers see under its name in its
scope: overload signatures the index folds, a declaration and its implementation, a name
declared twice; and nothing of a namesake in another scope."""

from __future__ import annotations

import itertools

import pytest

from server.code.app.memory_links import file_definitions
from server.code.domain.declaration import Declarations


def _hash(path: str, src: str, qualified_name: str) -> str:
    return next(
        d.body_hash
        for d in file_definitions(path, src.encode())
        if d.qualified_name == qualified_name
    )


_OVERLOADS = (
    "export function fetch_rows(a: number): number;\n"
    "export function fetch_rows(a: string): string;\n"
    "export function fetch_rows(a: any): any {\n  return a;\n}\n"
)
_METHODS = (
    "class C {\n  fetch_rows(a: number): number;\n  fetch_rows(a: string): string;\n"
    "  fetch_rows(a: any): any { return a; }\n}\n"
)
_DECLARED = (
    "declare function fetch_rows(a: number): number;\n"
    "function fetch_rows(a: number): number { return a; }\n"
)
_TWICE = "function fetch_rows() { return 1; }\nfunction fetch_rows() { return 2; }\n"


@pytest.mark.parametrize(
    ("path", "src", "qn", "old", "new"),
    [
        ("a.ts", _OVERLOADS, "a::fetch_rows", "(a: number): number;", "(a: number): boolean;"),
        ("b.ts", _METHODS, "b::C::fetch_rows", "(a: string): string;", "(a: string): boolean;"),
        ("c.ts", _DECLARED, "c::fetch_rows", "return a;", "return a + 1;"),
        ("d.js", _TWICE, "d::fetch_rows", "return 2", "return 3"),
        (
            "e.js",
            "var fetch_rows = () => 3, fetch_rows = () => 4;\n",
            "e::fetch_rows",
            "=> 4",
            "=> 5",
        ),
        (
            "f.ts",
            "var fetch_rows = () => 3,\n  fetch_rows = () => 4;\n",
            "f::fetch_rows",
            "=> 4",
            "=> 5",
        ),
    ],
)
def test_an_edit_anywhere_in_the_family_changes_the_fingerprint(
    path: str, src: str, qn: str, old: str, new: str
) -> None:
    assert _hash(path, src, qn) != _hash(path, src.replace(old, new), qn)


@pytest.mark.parametrize(
    ("path", "src", "qn", "old", "new"),
    [
        (
            "g.cpp",
            "int A::fetch_rows() { return 1; }\nint B::fetch_rows() { return 2; }\n",
            "A::fetch_rows",
            "return 2",
            "return 3",
        ),
        (
            "h.ts",
            "class C {\n  fetch_rows(): number { return 1; }\n  other(): number { return 1; }\n}\n",
            "h::C::fetch_rows",
            "other(): number { return 1",
            "other(): number { return 2",
        ),
    ],
)
def test_a_namesake_or_neighbour_is_not_in_the_family(
    path: str, src: str, qn: str, old: str, new: str
) -> None:
    assert _hash(path, src, qn) == _hash(path, src.replace(old, new), qn)


def test_the_family_lists_each_declaration_in_order() -> None:
    decls = Declarations.parse("a.ts", _OVERLOADS.encode())
    assert decls is not None
    keys = decls._family_keys([3], "fetch_rows", True)
    assert keys is not None
    family = [member for key in keys for member in decls._families[key]]
    assert len(family) == 3 and family[-1][0].endswith("return a;\n}")


def test_overload_signatures_sharing_a_line_are_in_the_family() -> None:
    src = (
        "export function fetch_rows(n: number): number; "
        "export function fetch_rows(n: string): number;\n"
        "export function fetch_rows(n: number | string | boolean): number { return 3; }\n"
    )
    edited = src.replace("(n: number): number;", "(n: boolean): number;")
    assert _hash("a.ts", src, "a::fetch_rows") != _hash("a.ts", edited, "a::fetch_rows")


def test_a_family_is_read_once_for_all_its_numbered_declarations(monkeypatch) -> None:
    # `fetch_rows`, `fetch_rows#2`, … are one family: its members are shaped once each
    calls = 0
    shape_at = Declarations._shape_at

    def counted(self: Declarations, *args: object) -> object:
        nonlocal calls
        calls += 1
        return shape_at(self, *args)  # type: ignore[arg-type]

    monkeypatch.setattr(Declarations, "_shape_at", counted)
    src = "".join(f"function fetch_rows() {{ return {i}; }}\n" for i in range(64))
    defs = file_definitions("m.js", src.encode())
    assert len(defs) == 64 and len({d.body_hash for d in defs}) == 1
    assert calls <= 2 * 64


@pytest.mark.parametrize(
    ("path", "src", "qualified_name"),
    [
        (
            "a.cpp",
            "#if USE_SSL\nint fetch_rows()\n{ return 1; }\n#else\nint fetch_rows()\n{ return 2; }\n#endif\n",
            "fetch_rows",
        ),
        (
            "a.py",
            "import sys\nif sys.version_info > (3,):\n    def fetch_rows():\n        return 1\n"
            "else:\n    def fetch_rows():\n        return 2\n",
            "a::fetch_rows",
        ),
        (
            "a.cpp",
            "namespace DB {\n#if X\nint fetch_rows() { return 1; }\n#elif Y\n"
            "int fetch_rows() { return 2; }\n#else\nint fetch_rows() { return 4; }\n#endif\n}\n",
            "DB::fetch_rows",
        ),
    ],
    ids=["cpp-if-else", "py-if-else", "cpp-elif"],
)
def test_a_name_defined_in_two_blocks_covers_both(path, src, qualified_name) -> None:
    before = _hash(path, src, qualified_name)
    assert _hash(path, src.replace("return 1", "return 7"), qualified_name) != before
    assert _hash(path, src.replace("return 2", "return 3"), qualified_name) != before


def test_a_rust_attribute_above_a_doc_comment_is_part_of_the_definition() -> None:
    src = '#[cfg(feature = "A")]\n/// Reads rows.\npub fn fetch_rows() -> u32 { 1 }\n'
    (d,) = file_definitions("src/a.rs", src.encode())
    assert d.start_line == 1
    assert _hash("src/a.rs", src.replace('"A"', '"B"'), d.qualified_name) != d.body_hash


def test_reading_a_class_is_linear_in_its_members() -> None:
    from tests.unit.step_count import steps

    def work(n: int) -> int:
        src = "class C:\n" + "".join(f"    def m{i}(self):\n        return {i}\n" for i in range(n))
        return steps(lambda: file_definitions("a.py", src.encode()))

    assert work(800) / work(100) < 12


def test_reading_a_namespace_is_linear_in_its_rows() -> None:
    from tests.unit.step_count import steps

    # each row ends on `;`, a token of the namespace body, not of the class
    def work(n: int) -> int:
        src = "namespace N {\n" + "".join(f"class C{i} {{ int m; }};\n" for i in range(n)) + "}\n"
        return steps(lambda: file_definitions("a.cpp", src.encode()))

    assert work(800) / work(100) < 12


@pytest.mark.parametrize(
    ("path", "src"),
    [
        ("src/a.rs", "#[inline]\n/// Reads rows.\npub fn fetch_rows() -> u32 { 1 }\n"),
        ("src/a.rs", "/// Reads rows.\npub fn fetch_rows() -> u32 { 1 }\n"),
        ("a.py", "@cache\n# Reads rows.\ndef fetch_rows():\n    return 1\n"),
        ("a.py", "# Reads rows.\ndef fetch_rows():\n    return 1\n"),
        ("a.py", "@a\n@b  # Reads rows.\ndef fetch_rows():\n    return 1\n"),
        ("src/a.rs", "#[inline]\n/* Reads rows. */\n#[cold]\npub fn fetch_rows() -> u32 { 1 }\n"),
        (
            "A.java",
            "class A {\n  @Override\n  // Reads rows.\n  public int fetchRows() { return 1; }\n}\n",
        ),
        ("a.ts", "@dec()\n// Reads rows.\nexport class Rows { x = 1; }\n"),
        ("a.cpp", "template <typename T>\n// Reads rows.\nint fetch_rows(T t) { return 1; }\n"),
        ("a.cpp", "[[nodiscard]]\n// Reads rows.\nint fetch_rows() { return 1; }\n"),
        (
            "a.cpp",
            "template <typename T>\nrequires true\n// Reads rows.\nint fetch_rows(T t) { return 1; }\n",
        ),
        ("a.ts", "export @dec()\n// Reads rows.\nclass Rows { x = 1; }\n"),
        ("a.cpp", "__attribute__((noinline))\n// Reads rows.\nint fetch_rows() { return 1; }\n"),
    ],
    ids=[
        "rust-attribute",
        "rust",
        "py-decorator",
        "py",
        "py-in-decorator",
        "rust-between",
        "java-annotation",
        "ts-class-decorator",
        "cpp-template",
        "cpp-attribute",
        "cpp-requires",
        "ts-export-decorator",
        "cpp-gnu-attribute",
    ],
)
def test_a_comment_above_a_definition_is_no_part_of_it(path, src) -> None:
    def own(source: str) -> list[tuple[str, str]]:
        defs = file_definitions(path, source.encode())
        return [
            (d.body_hash, d.shape) for d in defs if d.name in ("fetch_rows", "fetchRows", "Rows")
        ]

    assert own(src.replace("Reads rows", "Reads all rows")) == own(src) != []


def test_go_methods_on_two_receivers_are_two_families() -> None:
    src = (
        "package a\n\ntype A struct{}\ntype B struct{}\n\n"
        "func (a A) Read() int { return 1 }\nfunc (b *B) Read() int { return 2 }\n"
    )
    edited = src.replace("return 2", "return 3")
    assert _hash("pkg/a.go", edited, "pkg.a::A::Read") == _hash("pkg/a.go", src, "pkg.a::A::Read")
    assert _hash("pkg/a.go", edited, "pkg.a::B::Read") != _hash("pkg/a.go", src, "pkg.a::B::Read")


def test_an_inline_cpp_file_is_read_as_cpp() -> None:
    (d,) = file_definitions("src/a.inl", b"int fetch_rows() { return 1; }\n")
    assert d.shape.exact


def test_a_name_declared_many_times_is_hashed_once_for_all_its_numbers() -> None:
    from tests.unit.step_count import steps

    def work(n: int) -> int:
        src = "".join(f"function handler(a) {{ return a + {i}; }}\n" for i in range(n))
        return steps(lambda: file_definitions("lib/a.js", src.encode()))

    assert work(400) / work(50) < 12


def _defs(path: str, src: str, qualified_name: str) -> list:
    return [d for d in file_definitions(path, src.encode()) if d.qualified_name == qualified_name]


def _shown(decls: Declarations | None, line: int, name: str) -> str | None:
    m = decls.member(line, name, shapes=False) if decls else None
    return None if m is None else m.shown


def _conditions(shown: str | None) -> str:
    """The conditions heading what the judge reads, one a line."""
    heads = itertools.takewhile(
        lambda x: x.startswith("(selected when: "), (shown or "").splitlines()
    )
    return "\n".join(x.removeprefix("(selected when: ").removesuffix(")") for x in heads)


def _guard(path: str, src: str) -> str:
    """The conditions that select the first `value` in `src`."""
    decls = Declarations.parse(path, src.encode())
    assert decls is not None
    d = next(d for d in file_definitions(path, src.encode()) if d.name == "value")
    return _conditions(_shown(decls, d.start_line, "value"))


_SELECTED = "#if 1\nint value() { return 3; }\n#else\nint value() { return 4; }\n#endif\n"
_NESTED = "#ifdef A\n#if B\nint value() { return 3; }\n#endif\n#endif\n"
_PY_SELECTED = (
    "if True:\n    def value():\n        return 3\nelse:\n    def value():\n        return 4\n"
)


@pytest.mark.parametrize(
    ("path", "src", "qn", "old", "new"),
    [
        ("a.cpp", _SELECTED, "value", "#if 1", "#if 0"),
        ("b.cpp", _NESTED, "value", "#if B", "#if C"),
        ("c.cpp", _NESTED, "value", "#ifdef A", "#ifndef A"),
        ("d.py", _PY_SELECTED, "d::value", "if True", "if False"),
    ],
)
def test_a_condition_selecting_the_definition_changes_its_fingerprints(
    path: str, src: str, qn: str, old: str, new: str
) -> None:
    (before,), (after,) = _defs(path, src, qn)[:1], _defs(path, src.replace(old, new), qn)[:1]
    assert before.body_hash != after.body_hash
    assert before.shape[1:] != after.shape[1:]  # nor a rename of it
    renamed = _defs(path, src.replace("value", "amount"), qn.replace("value", "amount"))[0]
    assert renamed.shape[1:] == before.shape[1:]


def test_the_guard_names_each_condition_and_branch() -> None:
    src = "if A:\n    pass\nelif B:\n    def value():\n        return 3\nelse:\n    def value():\n        return 4\n"
    decls = Declarations.parse("a.py", src.encode())
    assert decls is not None
    assert _conditions(_shown(decls, 4, "value")) == "not if A\nelif B"
    assert _conditions(_shown(decls, 7, "value")) == "not if A\nnot elif B"
    nested = Declarations.parse("b.cpp", _NESTED.encode())
    assert nested is not None and _conditions(_shown(nested, 3, "value")) == "#ifdef A\n#if B"


def test_a_sibling_in_the_same_branch_stays_out_of_the_guard() -> None:
    src = "#if 1\nint value() { return 3; }\nint other() { return 5; }\n#endif\n"
    before = _defs("a.cpp", src, "value")[0]
    after = _defs("a.cpp", src.replace("return 5", "return 6"), "value")[0]
    assert before.body_hash == after.body_hash and _guard("a.cpp", src) == "#if 1"


@pytest.mark.parametrize(
    "head",
    [
        "# ifndef B_BAR_H\n#  define B_BAR_H\n",
        "#ifndef B_BAR_H // NOLINT(build/header_guard)\n#define B_BAR_H\n",
        "#if !defined(B_BAR_H)\n#define B_BAR_H\n",
    ],
)
def test_every_spelling_of_an_include_guard_is_no_condition(head: str) -> None:
    assert _guard("b/bar.h", f"{head}int value() {{ return 3; }}\n#endif\n") == ""


def test_a_directive_indented_after_its_hash_is_the_same_condition() -> None:
    body = "int value() { return 3; }\n#endif\n"
    flush, indented = (
        _defs("a.cpp", f"#if X\n{body}", "value"),
        _defs("a.cpp", f"#  if X\n{body}", "value"),
    )
    assert _guard("a.cpp", f"#if X\n{body}") == _guard("a.cpp", f"#  if X\n{body}") == "#if X"
    assert flush[0].body_hash == indented[0].body_hash


@pytest.mark.parametrize(
    ("path", "src"),
    [
        (
            "a.cpp",
            "#if "
            + " || ".join(f"defined(A{i})" for i in range(2000))
            + "\nint value() { return 3; }\n#endif\n",
        ),
        (
            "a.py",
            "if "
            + " or ".join(f"a{i}" for i in range(2000))
            + ":\n    def value():\n        return 3\n",
        ),
    ],
)
def test_a_condition_of_thousands_of_terms_is_read(path: str, src: str) -> None:
    assert "1999" in _guard(path, src)


def test_an_include_guard_is_no_condition() -> None:
    body = "int value() { return 3; }\n"
    guarded = _defs("a/foo.h", f"#ifndef A_FOO_H\n#define A_FOO_H\n{body}#endif\n", "value")[0]
    renamed = _defs("b/bar.h", f"#ifndef B_BAR_H\n#define B_BAR_H\n{body}#endif\n", "value")[0]
    once = _defs("c/baz.h", f"#pragma once\n{body}", "value")[0]
    assert _guard("a/foo.h", f"#ifndef A_FOO_H\n#define A_FOO_H\n{body}#endif\n") == ""
    assert guarded.body_hash == renamed.body_hash
    assert guarded.shape == once.shape


def test_a_comment_or_line_break_in_a_condition_is_no_change() -> None:
    src = "#if defined(A) && B\nint value() { return 3; }\n#endif\n"
    noted = src.replace(" && B", " /* why */ \\\n  && B")
    before, after = _defs("a.cpp", src, "value")[0], _defs("a.cpp", noted, "value")[0]
    assert _guard("a.cpp", src) == "#if defined ( A ) && B"
    assert (before.body_hash, before.shape[1:]) == (after.body_hash, after.shape[1:])


def test_a_guarded_file_is_read_in_linear_time() -> None:
    from tests.unit.step_count import steps

    def work(n: int) -> int:
        body = "".join(f"int value{i}() {{ return {i}; }}\n" for i in range(n))
        src = f"#if USE_X\n{body}#endif\n"
        return steps(lambda: file_definitions("a.cpp", src.encode()))

    assert work(400) / work(50) < 12


_HANDLED = (
    "try:\n    import fast\nexcept ValueError:\n    def value():\n        return 3\n"
    "except Exception:\n    def value():\n        return 4\n"
)


def test_the_handler_a_definition_is_in_is_its_guard() -> None:
    decls = Declarations.parse("a.py", _HANDLED.encode())
    assert decls is not None
    assert _conditions(_shown(decls, 4, "value")) == "except ValueError"
    assert _conditions(_shown(decls, 7, "value")) == "not except ValueError\nexcept Exception"


def test_another_handler_type_changes_the_fingerprints() -> None:
    before = _defs("a.py", _HANDLED, "a::value")[0]
    after = _defs("a.py", _HANDLED.replace("ValueError", "KeyError"), "a::value")[0]
    assert before.body_hash != after.body_hash
    assert before.shape[1:] != after.shape[1:]


def test_the_name_a_handler_binds_is_no_part_of_its_guard() -> None:
    bound = _HANDLED.replace("except ValueError:", "except (ValueError, OSError) as e:")
    renamed = bound.replace("as e:", "as err:")
    first, second = _defs("a.py", bound, "a::value")[0], _defs("a.py", renamed, "a::value")[0]
    assert _guard("a.py", bound) == "except ( ValueError , OSError )"
    assert (first.body_hash, first.shape[1:]) == (second.body_hash, second.shape[1:])


# Unchanged code under the current rules, one case per grammar's own rules. A hash change
# here ships with a `_RULES` bump, so stored links are re-hashed rather than compared
# across rules.
_PINNED = [
    (
        "a.py",
        "try:\n    import fast\nexcept ValueError as e:\n    def value():\n        return 3\n",
        "4ef2623536cb8a95",
        "e4551c91d7a3eeeb",
    ),
    (
        "b.cpp",
        "#ifndef B_H\n#define B_H\n#if defined(READY) // ready\nint value() { return 3; }\n"
        "#endif\n#endif\n",
        "d96ca32882a6d502",
        "1c9983388715966e",
    ),
    (
        "c.py",
        "if A:\n    pass\nelif B:\n    def value():\n        return 3\n",
        "94100921fc29fbd3",
        "89f7584a55ea9293",
    ),
    (
        "d.ts",
        "export function value(a: number): number {\n  return a + 3;\n}\n",
        "2cebd44d35d50d39",
        "f2510d1d465fe386",
    ),
    (
        "e.js",
        "class Box {\n  value() {\n    return 3;\n  }\n}\n",
        "0d033ce92cbd0dd6",
        "bc386ec8267445b9",
    ),
    (
        "f.go",
        "package f\n\ntype Box struct{}\n\nfunc (b Box) value() int {\n\treturn 3\n}\n",
        "ece58dcac5e37eb1",
        "3eff4fddcdae53cc",
    ),
    (
        "g.java",
        "class Box {\n  int value() {\n    return 3;\n  }\n}\n",
        "fe3460b82181bcec",
        "c2955123a94095f5",
    ),
    (
        "h.rs",
        "struct Box;\n\nimpl Box {\n    fn value(&self) -> i32 {\n        3\n    }\n}\n",
        "833cc756a2f30672",
        "c745fbc574ab7693",
    ),
]


@pytest.mark.parametrize(("path", "src", "body", "shape"), _PINNED)
def test_a_fingerprint_changes_only_with_its_rules(
    path: str, src: str, body: str, shape: str
) -> None:
    from server.code.domain.declaration import fingerprint_version

    (d,) = [d for d in file_definitions(path, src.encode()) if d.name == "value"]
    pinned = ("6", body, shape)
    assert (fingerprint_version().split("-")[0], d.body_hash[:16], d.shape.exact[:16]) == pinned, (
        "fingerprints changed: bump _RULES (a grammar upgrade alone needs none) and re-pin"
    )


def test_what_the_judge_reads_is_what_the_fingerprint_covers() -> None:
    (d,) = _defs("a.ts", _OVERLOADS, "a::fetch_rows")
    assert d.shown.split("\n\n") == [
        "export function fetch_rows(a: number): number;",
        "export function fetch_rows(a: string): string;",
        "export function fetch_rows(a: any): any {\n  return a;\n}",
    ]


def test_a_method_is_shown_as_written() -> None:
    (d,) = _defs("a.py", "class C:\n    def value(self):\n        return 3\n", "a::C::value")
    assert d.shown == "    def value(self):\n        return 3"


def test_a_reindented_literal_is_shown_as_changed() -> None:
    # the whole method four spaces further in: the literal's line too
    def program(pad: str) -> str:
        return f'class C:\n{pad}def value(self):\n{pad}    return """\n{pad}    x"""\n'

    (four,) = _defs("a.py", program("    "), "a::C::value")
    (eight,) = _defs("a.py", program("        "), "a::C::value")
    assert four.body_hash != eight.body_hash
    assert four.shown != eight.shown


def test_the_index_tree_is_reused_only_for_its_own_grammar() -> None:
    from server.code.domain.declaration import _parser

    src = b"def value():\n    return 3\n"
    tree = _parser("python").parse(src)
    own = Declarations.parse("a.py", src, tree)
    assert own is not None and own.root == tree.root_node
    other = Declarations.parse("a.ts", b"function value() { return 3; }\n", tree)
    assert other is not None and other.root != tree.root_node


def _value(path: str, src: str) -> str:
    return next(d.body_hash for d in file_definitions(path, src.encode()) if d.name == "value")


@pytest.mark.parametrize(
    "reformatted",
    [
        "int value(int a) { return a+1; }\n",
        "int value(int a)\n{\n    return a + 1;   \n}\n",
        "int value(int a) { /* one more */ return a + 1; }\n",
    ],
)
def test_a_reformat_outside_python_keeps_the_fingerprint(reformatted: str) -> None:
    original = "int value(int a) { return a + 1; }\n"
    assert _value("a.cpp", reformatted) == _value("a.cpp", original)


@pytest.mark.parametrize(
    "commented",
    [
        "fn value(a: i32) -> i32 { /* c */ a + 1 }\n",
        "fn value(a: i32) -> i32 {\n    // c\n    a + 1\n}\n",
        "fn value(a: i32) -> i32 { /* outer /* nested */ */ a + 1 }\n",
    ],
)
def test_a_rust_comment_keeps_the_fingerprint(commented: str) -> None:
    assert _value("a.rs", commented) == _value("a.rs", "fn value(a: i32) -> i32 { a + 1 }\n")


@pytest.mark.parametrize(
    ("path", "before", "after"),
    [
        (
            "a.cpp",
            'const char* value() { return "a b"; }\n',
            'const char* value() { return "a  b"; }\n',
        ),
        (
            "a.py",
            "def value():\n    if a:\n        b()\n    c()\n",
            "def value():\n    if a:\n        b()\n        c()\n",
        ),
    ],
)
def test_a_literal_or_python_indentation_still_changes_it(
    path: str, before: str, after: str
) -> None:
    assert _value(path, before) != _value(path, after)
