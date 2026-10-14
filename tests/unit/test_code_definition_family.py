"""A definition's fingerprint covers every declaration callers see under its name in its
scope: overload signatures the index folds, a declaration and its implementation, a name
declared twice; and nothing of a namesake in another scope."""

from __future__ import annotations

import itertools
import json
from typing import Any

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
        (
            "g.cpp",
            "int fetch_rows(int a = 1);\nint fetch_rows(int a) { return a; }\n",
            "fetch_rows",
            "= 1",
            "= 2",
        ),
        (
            "h.cpp",
            "const int& fetch_rows(int a = 1);\nconst int& fetch_rows(int a) { return a; }\n",
            "fetch_rows",
            "= 1",
            "= 2",
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
    assert len(family) == 3 and family[-1].shown.endswith("return a;\n}")


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
    shape = Declarations._shape

    def counted(self: Declarations, *args: object) -> object:
        nonlocal calls
        calls += 1
        return shape(self, *args)  # type: ignore[arg-type]

    monkeypatch.setattr(Declarations, "_shape", counted)
    src = "".join(f"function fetch_rows() {{ return {i}; }}\n" for i in range(64))
    defs = file_definitions("m.js", src.encode())
    assert len(defs) == 64 and len({d.body_hash for d in defs}) == 1
    assert calls <= 2 * 64


def test_a_member_is_walked_once(monkeypatch: Any) -> None:
    from server.code.domain import declaration

    calls: dict[str, int] = {"leaves": 0, "guard": 0}
    leaves, guard = declaration._leaves, Declarations._guard

    def counted_leaves(*args: Any) -> Any:
        calls["leaves"] += 1
        return leaves(*args)

    def counted_guard(self: Declarations, node: Any) -> str:
        calls["guard"] += 1
        return guard(self, node)

    monkeypatch.setattr(declaration, "_leaves", counted_leaves)
    monkeypatch.setattr(Declarations, "_guard", counted_guard)
    src = "".join(f"function f{i}() {{ return {i}; }}\n" for i in range(16))
    assert len(file_definitions("m.js", src.encode())) == 16
    assert calls == {"leaves": 16, "guard": 16}


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


def test_reading_many_declarators_of_one_binding_is_linear() -> None:
    from tests.unit.step_count import steps

    def work(n: int) -> int:
        src = "const " + ",\n  ".join(f"v{i} = () => {i} // c" for i in range(n)) + ";\n"
        return steps(lambda: file_definitions("a.js", src.encode()))

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
        "a9a9f013483a2132",
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
        "a1c613ad96b74bdc",
        "89f7584a55ea9293",
    ),
    (
        "d.ts",
        "export function value(a: number): number {\n  return a + 3;\n}\n",
        "8c8df964b10ecffa",
        "650acb7ba4221b54",
    ),
    (
        "e.js",
        "class Box {\n  value() {\n    return 3;\n  }\n}\n",
        "b3f3a6456b46a071",
        "e49ad6de2015e038",
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
    # comments, directives and docstrings: a change to how they are read moves these
    (
        "i.rs",
        "fn value() -> i32 {\n    /* outer /* inner */ still */ 3 // tail\n}\n",
        "4857960e7b53878f",
        "d1a6d50754acf60b",
    ),
    (
        "j.cpp",
        "int value() {\n  /* note */ return 3; // tail\n}\n",
        "eb331d01323ddc03",
        "8a58afdf3726bdbe",
    ),
    (
        "k.go",
        "package k\n\n//go:noinline\nfunc value() int {\n\t// plain\n\treturn 3\n}\n",
        "3f0252996c2d939f",
        "66edefd01a61f2c7",
    ),
    (
        "l.py",
        'def value():\n    """Three.\n\n        Always.\n    """\n    return 3  # tail\n',
        "6b1a2b541d435510",
        "854786c5ce006211",
    ),
]


@pytest.mark.parametrize(("path", "src", "body", "shape"), _PINNED)
def test_a_fingerprint_changes_only_with_its_rules(
    path: str, src: str, body: str, shape: str
) -> None:
    from server.code.domain.declaration import fingerprint_version

    (d,) = [d for d in file_definitions(path, src.encode()) if d.name == "value"]
    pinned = ("7", body, shape)
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


_PY = "def value(a, b):\n    x = g(a,b)  # one\n    if a:\n        return x\n    return y\n"


@pytest.mark.parametrize(
    "reformatted",
    [
        _PY.replace("# one", "# another"),
        _PY.replace("g(a,b)", "g(a, b)"),
        _PY.replace("    ", "  "),
        _PY.replace("g(a,b)", "g(\n        a, b\n    )").replace("    if", "\n    if"),
        _PY.replace("g(a,b)", "g(a, \\\n          b)"),
    ],
    ids=["comment", "spaces", "indent", "wrapped", "continuation"],
)
def test_a_python_reformat_or_comment_keeps_the_fingerprint(reformatted: str) -> None:
    assert _value("a.py", reformatted) == _value("a.py", _PY)


@pytest.mark.parametrize(
    ("path", "before", "after"),
    [
        (
            "a.cpp",
            'const char* value() { return "a b"; }\n',
            'const char* value() { return "a  b"; }\n',
        ),
        ("a.py", 'def value():\n    return "a b"\n', 'def value():\n    return "a  b"\n'),
        ("a.py", _PY, _PY.replace("    return y", "        return y")),
        (
            "a.py",
            "def value(xs):\n    for x in xs:\n        a(x)\n        b(x)\n",
            "def value(xs):\n    for x in xs:\n        a(x)\n    b(x)\n",
        ),
        (
            "a.py",
            'def value():\n    return """\n    x"""\n',
            'def value():\n    return """\n      x"""\n',
        ),
    ],
    ids=["cpp-literal", "py-literal", "into-if", "out-of-for", "multiline-string"],
)
def test_a_literal_or_a_python_block_change_still_changes_it(
    path: str, before: str, after: str
) -> None:
    assert _value(path, before) != _value(path, after)


@pytest.mark.parametrize(
    ("path", "original", "formatted"),
    [
        (
            "a.ts",
            "function value(a: number): number { return a + 1 }\n",
            "function value(a: number): number {\n  return a + 1;\n}\n",
        ),
        (
            "a.js",
            "function value(a) { let b = f('a', a,)\n  return [b,] }\n",
            'function value(a) {\n  let b = f("a", a);\n  return [b];\n}\n',
        ),
        ("a.js", "function value() { g();; h() }\n", "function value() { g(); h(); }\n"),
        (
            "a.ts",
            "class Box { n = 1\n  value() { return { a: 1, } } }\n",
            "class Box { n = 1;\n  value() { return { a: 1 }; }; }\n",
        ),
        (
            "a.ts",
            "function value(): { a: string, } { return { a: '' } }\n",
            'function value(): { a: string } { return { a: "" }; }\n',
        ),
        ("a.py", "def value(a):\n    return f('a', a)\n", 'def value(a):\n    return f("a", a)\n'),
        (
            "a.py",
            "def value(a):\n    return [U'a', F'{a}', Rb'\\d', '''x''']\n",
            'def value(a):\n    return [\n        "a",\n        f"{a}",\n        rb"\\d",\n        """x""",\n    ]\n',
        ),
        (
            "a.py",
            "def value(a, b):\n    return g(a, b), {a: b}, (a, b), x[a, b]\n",
            "def value(a, b,):\n    return g(a, b,), {a: b,}, (a, b,), x[a, b,]\n",
        ),
        (
            "a.rs",
            "fn value(a: i32) -> (i32, i32) { g(a, a); (a, a) }\n",
            "fn value(a: i32) -> (i32, i32,) { g(a, a,); (a, a,) }\n",
        ),
        (
            "a.go",
            "package a\n\nfunc value(a int) int { return f(a, a) }\n",
            "package a\n\nfunc value(a int) int {\n\treturn f(\n\t\ta,\n\t\ta,\n\t)\n}\n",
        ),
        (
            "a.java",
            "class A { int[] value() { return new int[] {1, 2}; } }\n",
            "class A { int[] value() { return new int[] {1, 2,}; } }\n",
        ),
        (
            "a.cpp",
            "int value() { return g({1, 2}); }\n",
            "int value() { return g({1, 2,}); }\n",
        ),
        (
            "a.py",
            "def value(a):\n    return a, 1\n",
            "def value(a):\n    return (\n        a,\n        1,\n    )\n",
        ),
        ("a.py", "def value(a):\n    return a,\n", "def value(a):\n    return (a,)\n"),
        (
            "a.py",
            "def value(x, y):\n    for k, v in x, y:\n        pass\n",
            "def value(x, y):\n    for (k, v) in (\n        x,\n        y,\n    ):\n        pass\n",
        ),
        (
            "a.py",
            "def value(a, b):\n    with open(a) as f, open(b) as g:\n        pass\n",
            "def value(a, b):\n    with (\n        open(a) as f,\n        open(b) as g,\n    ):\n        pass\n",
        ),
        (
            "a.py",
            "class C:\n  def value(self):\n    '''Doc\n    two.'''\n",
            'class C:\n    def value(self):\n        """Doc\n        two."""\n',
        ),
        (
            "a.py",
            'class C:\n    def value(self):\n        """Doc\n        two."""\n',
            'class B:\n    class C:\n        def value(self):\n            """Doc\n            two."""\n',
        ),
        (
            "a.py",
            'def value():\n    """  Doc.   \n\n      More.\n    """\n',
            'def value():\n    """Doc.\n\n    More.\n    """\n',
        ),
        (
            "a.py",
            "def value():\n    return 0XAB, 1E5, 10J\n",
            "def value():\n    return 0xAB, 1e5, 10j\n",
        ),
        (
            "a.py",
            "def value():\n    return 'it\\'s', \"a\\\"b\"\n",
            "def value():\n    return \"it's\", 'a\"b'\n",
        ),
        ("a.ts", "function value() { return 'it\\'s' }\n", 'function value() { return "it\'s" }\n'),
        ("a.ts", "function value() { return 0XFF }\n", "function value() { return 0xff }\n"),
        (
            "a.ts",
            "interface value { a: number, b: string }\n",
            "interface value {\n  a: number;\n  b: string;\n}\n",
        ),
        ("a.ts", "type value = 'a' | 'b'\n", "type value =\n  | 'a'\n  | 'b';\n"),
        ("a.ts", "function value() { return new A }\n", "function value() { return new A() }\n"),
        (
            "a.rs",
            "fn value(x: u8) -> u8 { match x { 1 => { 2 }, _ => 3 } }\n",
            "fn value(x: u8) -> u8 {\n    match x {\n        1 => {\n            2\n        }\n"
            "        _ => 3,\n    }\n}\n",
        ),
    ],
    ids=[
        "ts-semicolon",
        "js-quotes-commas",
        "js-empty-statement",
        "ts-class-members",
        "ts-type-literal",
        "py-quotes",
        "py-prefixes",
        "py-commas",
        "rust-commas",
        "go-commas",
        "java-comma",
        "cpp-comma",
        "py-wrapped-tuple",
        "py-one-tuple",
        "py-for-tuples",
        "py-with-parentheses",
        "py-docstring-indent",
        "py-docstring-depth",
        "py-docstring-cleanup",
        "py-number-case",
        "py-quote-escapes",
        "ts-quote-escape",
        "ts-number-case",
        "ts-interface-separators",
        "ts-union-leading-bar",
        "ts-new-parentheses",
        "rust-match-arm-comma",
    ],
)
def test_a_formatters_quotes_commas_and_semicolons_keep_the_fingerprint(
    path: str, original: str, formatted: str
) -> None:
    assert _value(path, formatted) == _value(path, original)


@pytest.mark.parametrize(
    ("path", "before", "after"),
    [
        ("a.py", "def value():\n    return 'a'\n", "def value():\n    return 'b'\n"),
        ("a.py", "def value(a):\n    return (a,)\n", "def value(a):\n    return (a)\n"),
        ("a.py", "def value(x, a):\n    return x[a,]\n", "def value(x, a):\n    return x[a]\n"),
        ("a.py", "def value():\n    return r'\\d'\n", "def value():\n    return '\\d'\n"),
        ("a.py", "def value():\n    return b'a'\n", "def value():\n    return 'a'\n"),
        ("a.rs", "fn value(a: (i32,)) {}\n", "fn value(a: (i32)) {}\n"),
        ("a.rs", "fn value() -> u32 { m!(a,) }\n", "fn value() -> u32 { m!(a) }\n"),
        ("a.cpp", "int value() { return F(a,); }\n", "int value() { return F(a); }\n"),
        ("a.cpp", "char value() { return 'a'; }\n", 'char value() { return "a"; }\n'),
        ("a.js", "function value() { return `a` }\n", "function value() { return 'a' }\n"),
        ("a.js", "function value() { return [a,,] }\n", "function value() { return [a,] }\n"),
        (
            "a.js",
            "function value() { a = b\n  (c) }\n",
            "function value() { a = b;\n  (c) }\n",
        ),
        (
            "a.js",
            "function value() { if (x); f() }\n",
            "function value() { if (x) f() }\n",
        ),
        (
            "a.py",
            "def value():\n    return 'SELECT 1\\n'\n",
            "def value():\n    return 'DROP TABLE x\\n'\n",
        ),
        ("a.py", "def value():\n    return ' \\n'\n", "def value():\n    return '  \\n'\n"),
        (
            "a.py",
            'def value():\n    """Return rows.\\n"""\n    return 1\n',
            'def value():\n    """Delete rows.\\n"""\n    return 1\n',
        ),
        (
            "a.py",
            "def value(x):\n    return f'{x:.2f}'\n",
            "def value(x):\n    return f'{x:.3f}'\n",
        ),
        ("a.py", "def value(x):\n    return f'{x: >4}'\n", "def value(x):\n    return f'{x:>4}'\n"),
        (
            "a.py",
            "def value(x):\n    return f'rows: {x}\\n'\n",
            "def value(x):\n    return f'cols: {x}\\n'\n",
        ),
        (
            "a.py",
            "def value():\n    return b'\\x00abc'\n",
            "def value():\n    return b'\\x00xyz'\n",
        ),
        (
            "a.py",
            "def value(a, b):\n    return g((a, b))\n",
            "def value(a, b):\n    return g(a, b)\n",
        ),
        ("a.py", "def value():\n    return ()\n", "def value():\n    return\n"),
        (
            "a.py",
            'def value():\n    """Doc.\n\n    a\n      b\n    """\n',
            'def value():\n    """Doc.\n\n    a\n    b\n    """\n',
        ),
        ("a.py", "def value():\n    return 0xAB\n", "def value():\n    return 0xAC\n"),
        ("a.py", "def value():\n    return r'a\\'b'\n", 'def value():\n    return r"a\'b"\n'),
        ("a.py", "def value(x):\n    return f'{x=}'\n", "def value(x):\n    return f'{x = }'\n"),
        (
            "a.py",
            "def value(x):\n    return f'{x+1=}'\n",
            "def value(x):\n    return f'{x + 1=}'\n",
        ),
        (
            "a.ts",
            "function value() { return new A().b }\n",
            "function value() { return new A.b }\n",
        ),
        (
            "a.ts",
            "function value() { return new (a()) }\n",
            "function value() { return new a() }\n",
        ),
    ],
    ids=[
        "py-literal",
        "py-one-tuple",
        "py-tuple-index",
        "py-raw",
        "py-bytes",
        "rust-one-tuple",
        "rust-macro",
        "cpp-macro-argument",
        "cpp-char",
        "js-template",
        "js-hole",
        "js-asi",
        "js-empty-body",
        "py-text-beside-escape",
        "py-spaces-beside-escape",
        "py-docstring-beside-escape",
        "py-format-spec",
        "py-format-fill",
        "py-fstring-beside-escape",
        "py-bytes-beside-escape",
        "py-tuple-argument",
        "py-empty-tuple",
        "py-docstring-relative-indent",
        "py-number",
        "py-raw-escape",
        "py-fstring-debug-spaces",
        "py-fstring-debug-expression",
        "ts-new-member",
        "ts-new-call",
    ],
)
def test_a_comma_quote_or_semicolon_that_means_something_changes_it(
    path: str, before: str, after: str
) -> None:
    assert _value(path, before) != _value(path, after)


def test_an_escape_other_than_a_quotes_changes_it() -> None:
    # beside a quote's escape, the text between the quotes is hashed as written
    before = "def value():\n    return 'a\\tb'\n"
    after = "def value():\n    return 'a\tb'\n"
    assert _value("a.py", before) != _value("a.py", after)


def _notebook(body: str) -> bytes:
    cells = [
        {"cell_type": "markdown", "source": ["# Rows"]},
        {"cell_type": "code", "source": ["import os\n"]},
        {"cell_type": "code", "source": body.splitlines(keepends=True)},
    ]
    return json.dumps({"cells": cells, "nbformat": 4}).encode()


def test_a_notebook_definition_is_fingerprinted_from_its_code_cell() -> None:
    from server.code.app.memory_links import file_at
    from server.code.domain.successor import Side

    before = file_at("a.ipynb", _notebook("def value(a):\n    return a + 1\n"))
    after = file_at("a.ipynb", _notebook("def value(a):\n    return a + 2\n"))
    (was,), (now,) = ([d for d in f.defs if d.name == "value"] for f in (before, after))
    assert was.body_hash and was.body_hash != now.body_hash
    assert was.shape.exact and was.shown == "def value(a):\n    return a + 1"
    assert Side("a.ipynb", before.defs, before.lines).text(was) == was.shown


_SPLIT_HEAD = (
    "#if !defined(X)\nint A::compareAt(int n) const\n#else\nint A::doCompareAt(int n) const\n"
    "#endif\n{\n    return n;\n}\n"
)


def test_a_declaration_parse_recovery_made_is_fingerprinted() -> None:
    # a function head split by `#if` parses as two declarations and a lone body
    decls = Declarations.parse("a.cpp", _SPLIT_HEAD.encode())
    assert decls is not None
    decls.fingerprints([4], "doCompareAt")
    defs = file_definitions("a.cpp", _SPLIT_HEAD.encode())
    assert all(d.body_hash for d in defs)


def test_a_file_that_fails_to_fingerprint_is_a_parse_failure(monkeypatch: Any) -> None:
    from server.code.app import memory_links

    def broken(*_: Any, **__: Any) -> None:
        raise TypeError("boom")

    monkeypatch.setattr(memory_links, "definitions", broken)
    with pytest.raises(memory_links.ParseFailed):
        memory_links.file_at("a.py", b"def value():\n    return 1\n")


_CLASS_TEMPLATE = "template <class T = int>\nclass value {\n  T n;\n};\n"


def test_a_class_templates_head_on_its_own_line_is_part_of_it() -> None:
    (d,) = file_definitions("a.cpp", _CLASS_TEMPLATE.encode())
    assert d.start_line == 1 and d.shown.startswith("template <class T = int>\n")
    assert _value("a.cpp", _CLASS_TEMPLATE.replace("int", "long")) != d.body_hash
    assert _value("a.cpp", _CLASS_TEMPLATE.replace(">\nclass", "> class")) == d.body_hash


_GO = "package a\n\n"


@pytest.mark.parametrize(
    ("path", "original", "reformatted", "edited"),
    [
        (
            "a.go",
            _GO + "type value struct{ n int }\n",
            _GO + "type value struct {\n\tn int // count\n}\n",
            _GO + "type value struct{ n string }\n",
        ),
        (
            "a.go",
            _GO + "var value = f(1,2)\n",
            _GO + "var value = f(1, 2) // two\n",
            _GO + "var value = f(1, 3)\n",
        ),
        (
            "a.go",
            _GO + "type value interface{ M() }\n",
            _GO + "type value interface {\n\tM()\n}\n",
            _GO + "type value interface{ N() }\n",
        ),
        ("a.py", "value = f(1,2)\n", "value = f(1, 2)  # c\n", "value = f(1, 3)\n"),
        (
            "a.py",
            "class C:\n    value: int = f(1,2)\n",
            "class C:\n    value: int = f(\n        1, 2\n    )\n",
            "class C:\n    value: str = f(1,2)\n",
        ),
    ],
    ids=["go-struct", "go-var", "go-interface", "py-constant", "py-class-constant"],
)
def test_a_type_or_constant_reformatted_keeps_its_fingerprint(
    path: str, original: str, reformatted: str, edited: str
) -> None:
    (was,) = [d for d in file_definitions(path, original.encode()) if d.name == "value"]
    assert was.shape.exact
    assert _value(path, reformatted) == was.body_hash
    assert _value(path, edited) != was.body_hash


@pytest.mark.parametrize(
    ("path", "original", "formatted"),
    [
        ("a.js", "const value = a => a + 1;\n", "const value = (a) => a + 1;\n"),
        ("a.ts", "const value = a => a + 1;\n", "const value = (a) => a + 1;\n"),
        (
            "a.js",
            "function value(a, b, c) { return a && b || c; }\n",
            "function value(a, b, c) {\n  return (a && b) || c;\n}\n",
        ),
        (
            "a.py",
            "def value(a, b):\n    return a and b\n",
            "def value(a, b):\n    return (\n        a and b\n    )\n",
        ),
        (
            "a.jsx",
            "function value() { return <div>x</div> }\n",
            "function value() {\n  return (\n    <div>x</div>\n  );\n}\n",
        ),
    ],
    ids=["js-arrow-param", "ts-arrow-param", "js-mixed-logic", "py-wrapped-return", "jsx"],
)
def test_parentheses_a_formatter_adds_keep_the_fingerprint(
    path: str, original: str, formatted: str
) -> None:
    assert _value(path, formatted) == _value(path, original)


@pytest.mark.parametrize(
    ("path", "before", "after"),
    [
        (
            "a.js",
            "function value(a) { return (a?.b).c }\n",
            "function value(a) { return a?.b.c }\n",
        ),
        (
            "a.js",
            "function value(a, b, c) { return a && (b || c) }\n",
            "function value(a, b, c) { return a && b || c }\n",
        ),
        (
            "a.py",
            "def value(a, b):\n    return (a + b) * 2\n",
            "def value(a, b):\n    return a + b * 2\n",
        ),
        (
            "a.js",
            "function value(a, b) { return f((a, b)) }\n",
            "function value(a, b) { return f(a, b) }\n",
        ),
        ("a.ts", "const value = (a: number) => a;\n", "const value = (a) => a;\n"),
        ("a.js", "const value = () => ({});\n", "const value = () => {};\n"),
        (
            "a.cpp",
            "auto value(int x) -> decltype((x)) { return x; }\n",
            "auto value(int x) -> decltype(x) { return x; }\n",
        ),
    ],
    ids=[
        "optional-chain",
        "precedence",
        "py-precedence",
        "comma-operator",
        "typed-param",
        "object-body",
        "decltype",
    ],
)
def test_parentheses_that_mean_something_change_it(path: str, before: str, after: str) -> None:
    assert _value(path, before) != _value(path, after)


@pytest.mark.parametrize(
    ("path", "before", "after"),
    [
        (
            "a.go",
            "package a\n\n//go:noinline\nfunc value() int { return 1 }\n",
            "package a\n\nfunc value() int { return 1 }\n",
        ),
        (
            "a.go",
            "package a\n\n//go:embed a.txt\nvar value string\n",
            "package a\n\n//go:embed b.txt\nvar value string\n",
        ),
        (
            "a.py",
            "def value():\n    x = []  # type: list[int]\n    return x\n",
            "def value():\n    x = []  # type: list[str]\n    return x\n",
        ),
    ],
    ids=["go-directive", "go-embed", "py-type-comment"],
)
def test_a_directive_comment_changes_it(path: str, before: str, after: str) -> None:
    assert _value(path, before) != _value(path, after)


def test_a_go_directive_starts_its_definition_and_its_doc_comment_does_not() -> None:
    src = "package a\n\n// value is one.\n//go:noinline\nfunc value() int { return 1 }\n"
    (d,) = file_definitions("a.go", src.encode())
    assert d.start_line == 4 and d.shown.startswith("//go:noinline\nfunc value()")
    assert _value("a.go", src.replace("is one", "is two")) == d.body_hash
